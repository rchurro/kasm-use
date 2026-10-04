"""Can Clef-Flash pick click targets on Kasm screenshots, fast enough, on this GPU?

An offline experiment, not part of kasm-use. Nothing here talks to Kasm. Screenshots get numbered
boxes drawn over them (OCR text boxes, plus grid cells where OCR found nothing, for icons), and
Clef is asked which box to click for a goal and what kind of page it is looking at. The report
gives accuracy, decision time, GPU memory and invalid outputs.

  python scripts/clef_check.py env                          GPU / library check
  python scripts/clef_check.py marks cases.json --out marked  draw the boxes only (no GPU needed)
  python scripts/clef_check.py run cases.json --out results   ask Clef (local GPU)
  python scripts/clef_check.py run cases.json --out results --ollama-model <tag>   via Ollama instead

cases.json — x/y are pixels in the original screenshot; "page" is optional:
  [{"image": "shots/search.png", "page": "normal",
    "clicks": [{"goal": "the Search button", "x": 812, "y": 64}]}]
  page is one of: normal, login, captcha, payment, loading

Install (Linux, NVIDIA driver + CUDA PyTorch):
  pip install torch transformers accelerate huggingface_hub bitsandbytes pillow numpy rapidocr

Precision: --precision nf4 (default) quantises to 4-bit so the 9B model fits in 8 GB. It loads the
full fp16 model into system RAM first (~18 GB), then moves it to the GPU layer by layer as 4-bit.
bf16 needs an RTX 30-series or newer; fp16 on older cards can overflow, which shows up as
"invalid" outputs in the report.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path

# Pinned checkpoint and the SHA-256 of its model code, as reviewed by the jevdash project
# (github.com/Sunwood-ai-labs/jevdash, PR 1). Clef runs this code on load; refuse anything else.
REPO = "Cloudflare/clef-flash"
REVISION = "17f0b0ad64efb65d273590632833508766b2aae6"
CODE_SHA = "0e304cf7c6500e8bb59bef7e2afd2c6373f82596dfb3b57d1aa93c175e2dc3a3"

PAGES = {
    "normal": "An ordinary page or app screen the agent can keep working on.",
    "login": "Asks for a username, password, MFA or one-time code.",
    "captcha": "A CAPTCHA, 'are you human' check, or bot challenge.",
    "payment": "Asks for card, bank or other payment details.",
    "loading": "Blank, spinner or still loading; nothing to act on yet.",
}


# --------------------------------------------------------------------------- marks (pure logic)
def ocr_marks(items, min_score: float = 0.5, max_marks: int = 60) -> list[dict]:
    """OCR results [(x0, y0, x1, y1, text, score)] -> numbered marks in reading order."""
    kept = [i for i in items if i[5] >= min_score and i[4].strip()]
    kept = sorted(kept, key=lambda i: (i[2] - i[0]) * (i[3] - i[1]), reverse=True)[:max_marks]
    # Reading order: boxes whose vertical centres are within half a line of each other share a row.
    kept.sort(key=lambda i: (i[1] + i[3]) / 2)
    rows: list[list] = []
    for i in kept:
        cy, h = (i[1] + i[3]) / 2, i[3] - i[1]
        if rows and abs(cy - rows[-1][0]) <= max(h, rows[-1][1]) / 2:
            rows[-1][2].append(i)
        else:
            rows.append([cy, h, [i]])
    ordered = [i for _, _, row in rows for i in sorted(row, key=lambda i: i[0])]
    return [{"id": str(n), "box": tuple(int(v) for v in i[:4]), "text": i[4].strip()[:40]}
            for n, i in enumerate(ordered, 1)]


def grid_marks(width: int, height: int, cols: int, rows: int, taken: list[dict], start: int,
               busy=lambda box: True) -> list[dict]:
    """Grid cells with no OCR box centre that `busy` says hold something, for icon-only controls."""
    centres = [((m["box"][0] + m["box"][2]) / 2, (m["box"][1] + m["box"][3]) / 2) for m in taken]
    out = []
    for r in range(rows):
        for c in range(cols):
            box = (c * width // cols, r * height // rows, (c + 1) * width // cols, (r + 1) * height // rows)
            if not any(box[0] <= x < box[2] and box[1] <= y < box[3] for x, y in centres) and busy(box):
                out.append({"id": str(start + len(out)), "box": box, "text": ""})
    return out


def mark_at(marks: list[dict], x: float, y: float, pad: int = 4) -> list[str]:
    """Ids of every mark whose box (padded) contains the point."""
    return [m["id"] for m in marks
            if m["box"][0] - pad <= x <= m["box"][2] + pad and m["box"][1] - pad <= y <= m["box"][3] + pad]


def valid_probs(probs: dict, options) -> bool:
    vals = list(probs.values())
    return (set(probs) == set(options) and all(isinstance(p, float) and math.isfinite(p) and 0 <= p <= 1 for p in vals)
            and abs(sum(vals) - 1) < 1e-3)


def parse_choice(text: str, options) -> str | None:
    """First option key that appears as a whole word in a model's text answer."""
    for token in re.findall(r"[A-Za-z0-9_]+", text or ""):
        if token in options or token.lower() in options:
            return token if token in options else token.lower()
    return None


def percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0.0


# ------------------------------------------------------------------------------------ images
def run_ocr(image) -> list[tuple]:
    import numpy as np
    arr = np.asarray(image.convert("RGB"))
    try:  # rapidocr >= 2
        from rapidocr import RapidOCR
        res = RapidOCR()(arr)
        rows = zip(res.boxes if res.boxes is not None else [], res.txts or [], res.scores or [])
    except ImportError:  # rapidocr-onnxruntime 1.x
        from rapidocr_onnxruntime import RapidOCR
        found, _ = RapidOCR()(arr)
        rows = ((b, t, s) for b, t, s in (found or []))
    out = []
    for quad, text, score in rows:
        xs, ys = [p[0] for p in quad], [p[1] for p in quad]
        out.append((min(xs), min(ys), max(xs), max(ys), str(text), float(score)))
    return out


def build_marks(image, mode: str, cols: int, rows: int, max_marks: int) -> list[dict]:
    from PIL import ImageDraw, ImageStat
    marks = ocr_marks(run_ocr(image), max_marks=max_marks) if mode in ("ocr", "both") else []
    if mode in ("grid", "both"):
        # Paint out OCR'd text, then keep only cells whose remaining pixels vary (something drawn there).
        grey = image.convert("L")
        draw = ImageDraw.Draw(grey)
        for m in marks:
            draw.rectangle(m["box"], fill=grey.getpixel((m["box"][0], m["box"][1])))
        marks += grid_marks(image.width, image.height, cols, rows, marks, len(marks) + 1,
                            busy=lambda box: ImageStat.Stat(grey.crop(box)).stddev[0] > 6)
    return marks


def draw_marks(image, marks: list[dict], width: int):
    """Boxes and big numbers on a copy, then scaled to `width` so the labels stay readable."""
    from PIL import ImageDraw, ImageFont
    scale = width / image.width
    img = image.convert("RGB").resize((width, round(image.height * scale)))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    for m in marks:
        x0, y0, x1, y1 = (round(v * scale) for v in m["box"])
        colour = (230, 30, 30) if m["text"] else (30, 90, 230)
        draw.rectangle((x0, y0, x1, y1), outline=colour, width=2)
        tw, th = draw.textbbox((0, 0), m["id"], font=font)[2:]
        # Text boxes get their number just above (or below) so it doesn't hide the text Clef reads.
        ly = y0 if not m["text"] else (y0 - th - 3 if y0 - th - 3 >= 0 else y1 + 1)
        draw.rectangle((x0, ly, x0 + tw + 4, ly + th + 2), fill=colour)
        draw.text((x0 + 2, ly), m["id"], fill=(255, 255, 255), font=font)
    return img


def click_question(goal: str, marks: list[dict]) -> dict:
    return {"type": "choice",
            "instructions": "The image is a remote desktop screenshot with numbered boxes drawn over it. Red boxes "
                            "hold text found by OCR; blue boxes are areas with no text, such as icons. Choose the "
                            f"box to click to: {goal}. Judge by what is inside the box, not by the number.",
            "criteria": {m["id"]: f"Box {m['id']}: " + (f"text '{m['text']}'" if m["text"] else "no text (icon or empty area)")
                         for m in marks}}


def page_question() -> dict:
    return {"type": "choice", "instructions": "What kind of screen is this remote desktop showing right now?",
            "criteria": dict(PAGES)}


# ---------------------------------------------------------------------------------- backends
class LocalClef:
    def __init__(self, precision: str, revision: str, code_sha: str, max_tokens: int):
        import torch
        from huggingface_hub import snapshot_download
        self.torch, self.max_tokens = torch, max_tokens
        if not torch.cuda.is_available():
            sys.exit("No CUDA GPU visible to PyTorch.")
        started = time.perf_counter()
        path = snapshot_download(REPO, revision=revision)
        sha = hashlib.sha256((Path(path) / "joint_schema_model.py").read_bytes()).hexdigest()
        if sha != code_sha:
            sys.exit(f"joint_schema_model.py hash {sha} != expected {code_sha}; refusing to run unreviewed code.")
        sys.path.insert(0, path)
        from joint_schema_model import collate_records, encode_record, load_release_model
        self.collate, self.encode = collate_records, encode_record
        if precision == "nf4":
            model, self.processor = load_release_model(path, device="cpu", dtype=torch.float16, attn_implementation="sdpa")
            self.model = self._to_4bit(model).cuda()
        else:
            dtype = torch.bfloat16 if precision == "bf16" else torch.float16
            self.model, self.processor = load_release_model(path, device="cuda", dtype=dtype, attn_implementation="sdpa")
        self.model.eval()
        self.load_s = time.perf_counter() - started

    def _to_4bit(self, model):
        """Swap large Linear layers for bitsandbytes NF4 ones; they quantise on .cuda().

        Output heads stay in fp16: they produce the choice logits, and they are small.
        """
        import bitsandbytes as bnb
        torch = self.torch
        skip = re.compile(r"(^|\.)(lm_head|head|heads|classifier|score)(\.|$)")

        def walk(module, prefix=""):
            for name, child in list(module.named_children()):
                full = f"{prefix}.{name}" if prefix else name
                if (isinstance(child, torch.nn.Linear) and not skip.search(full)
                        and child.in_features * child.out_features >= 1 << 20):
                    q = bnb.nn.Linear4bit(child.in_features, child.out_features, bias=child.bias is not None,
                                          compute_dtype=torch.float16, quant_type="nf4")
                    q.weight = bnb.nn.Params4bit(child.weight.data.to(torch.float16), requires_grad=False, quant_type="nf4")
                    if child.bias is not None:
                        q.bias = torch.nn.Parameter(child.bias.data.to(torch.float16), requires_grad=False)
                    setattr(module, name, q)
                else:
                    walk(child, full)
        walk(model)
        return model

    def ask(self, image, question: dict, state: dict) -> dict:
        torch = self.torch
        t0 = time.perf_counter()
        record = {"id": "q", "state": state, "images": [image.convert("RGB")], "questions": {"q": question}}
        enc = self.encode(self.processor.tokenizer, record, processor=self.processor, max_length=1_000_000)
        tokens = len(enc.input_ids)
        if tokens > self.max_tokens:
            return {"error": f"{tokens} input tokens > --max-tokens {self.max_tokens}", "tokens": tokens}
        pad = self.processor.tokenizer.pad_token_id
        batch = self.collate([enc], pad if pad is not None else self.processor.tokenizer.eos_token_id, torch.device("cuda"))
        torch.cuda.synchronize()
        t1 = time.perf_counter()
        with torch.inference_mode():
            logits = self.model(batch)[0]
        torch.cuda.synchronize()
        t2 = time.perf_counter()
        probs = dict(zip(enc.questions[0].option_ids, logits[0].float().softmax(-1).tolist()))
        out = {"tokens": tokens, "model_ms": (t2 - t1) * 1000, "total_ms": (t2 - t0) * 1000, "probs": probs}
        if not valid_probs(probs, question["criteria"]):
            out["error"] = "invalid probabilities (NaN/inf or wrong options) — try another --precision"
        else:
            out["choice"] = max(probs, key=probs.get)
            out["confidence"] = probs[out["choice"]]
        return out

    def peak_mb(self) -> float:
        return self.torch.cuda.max_memory_allocated() / 2**20


class OllamaClef:
    """Same questions through Ollama's chat API. Text answers only: no per-option probabilities."""

    def __init__(self, model: str, url: str):
        self.model, self.url, self.load_s = model, url.rstrip("/"), 0.0

    def ask(self, image, question: dict, state: dict) -> dict:
        buf = io.BytesIO()
        image.convert("RGB").save(buf, "PNG")
        options = "\n".join(f"{k}: {v}" for k, v in question["criteria"].items())
        prompt = (f"{question['instructions']}\n\nContext: {json.dumps(state)}\n\nOptions:\n{options}\n\n"
                  "Answer with only the option key.")
        body = {"model": self.model, "stream": False, "options": {"temperature": 0},
                "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(buf.getvalue()).decode()]}]}
        req = urllib.request.Request(self.url + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
        t0 = time.perf_counter()
        reply = json.loads(urllib.request.urlopen(req, timeout=120).read())
        ms = (time.perf_counter() - t0) * 1000
        text = (reply.get("message") or {}).get("content", "")
        choice = parse_choice(text, question["criteria"])
        out = {"model_ms": ms, "total_ms": ms, "raw": text[:200]}
        if choice is None:
            out["error"] = "answer matched no option"
        else:
            out["choice"] = choice
        return out

    def peak_mb(self) -> float:
        return 0.0


# ---------------------------------------------------------------------------------- commands
def cmd_env(_args) -> None:
    import importlib.util
    print(f"python        {sys.version.split()[0]}")
    try:
        import torch
        print(f"torch         {torch.__version__}  cuda={torch.version.cuda}  available={torch.cuda.is_available()}")
        if torch.cuda.is_available():
            p = torch.cuda.get_device_properties(0)
            native_bf16 = p.major >= 8
            print(f"gpu           {p.name}  {p.total_memory / 2**30:.1f} GB  compute {p.major}.{p.minor}  "
                  f"native bf16={native_bf16}")
            if not native_bf16:
                print("              -> use --precision nf4 (default) or fp16; bf16 would be emulated and slow")
            if p.total_memory < 16 * 2**30:
                print("              -> under 16 GB: only nf4 will fit Clef-Flash 9B")
    except ImportError:
        print("torch         MISSING")
    for mod in ("transformers", "accelerate", "huggingface_hub", "bitsandbytes", "PIL", "numpy", "rapidocr",
                "rapidocr_onnxruntime"):
        spec = importlib.util.find_spec(mod)
        version = ""
        if spec:
            try:
                version = getattr(__import__(mod), "__version__", "")
            except Exception as e:  # e.g. bitsandbytes without CUDA libs
                version = f"(import failed: {e})"
        print(f"{mod:<13} {version if spec else 'missing'}")
    try:
        mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        avail = int(mem["MemAvailable"].split()[0]) / 2**20
        print(f"system RAM    {avail:.1f} GB available" + ("  -> nf4 loading needs ~18 GB" if avail < 20 else ""))
    except OSError:
        pass


def load_cases(path: str) -> list[dict]:
    cases = json.loads(Path(path).read_text())
    base = Path(path).parent
    for c in cases:
        c["path"] = str((base / c["image"]).resolve())
        if c.get("page") and c["page"] not in PAGES:
            sys.exit(f"{c['image']}: page must be one of {', '.join(PAGES)}")
    return cases


def cmd_marks(args) -> None:
    from PIL import Image
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    covered = total = 0
    for c in load_cases(args.cases):
        img = Image.open(c["path"])
        marks = build_marks(img, args.marks, args.grid_cols, args.grid_rows, args.max_marks)
        draw_marks(img, marks, args.width).save(out / (Path(c["image"]).stem + ".marked.png"))
        for click in c.get("clicks", []):
            hit = mark_at(marks, click["x"], click["y"])
            total += 1
            covered += bool(hit)
            print(f"{c['image']}: '{click['goal']}' -> boxes {hit or 'NONE (no box covers this point)'}")
        print(f"{c['image']}: {len(marks)} boxes")
    print(f"\nBoxes cover {covered}/{total} click targets. Check the images in {out}/ before spending GPU time.")


def cmd_run(args) -> None:
    from PIL import Image
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cases = load_cases(args.cases)
    clef = (OllamaClef(args.ollama_model, args.ollama_url) if args.ollama_model
            else LocalClef(args.precision, args.revision, args.code_sha, args.max_tokens))
    print(f"model ready in {clef.load_s:.1f} s")

    # Warm-up so the first timed question doesn't include CUDA kernel setup.
    first = Image.open(cases[0]["path"])
    for _ in range(args.warmup):
        clef.ask(draw_marks(first, [], args.width), page_question(), {"screen": "warm-up"})

    rows, clicks_ok, clicks, covered, pages_ok, pages = [], 0, 0, 0, 0, 0
    for c in cases:
        img = Image.open(c["path"])
        marks = build_marks(img, args.marks, args.grid_cols, args.grid_rows, args.max_marks)
        marked = draw_marks(img, marks, args.width)
        state = {"screen": f"{img.width}x{img.height}", "boxes": len(marks)}
        for click in c.get("clicks", []):
            r = clef.ask(marked, click_question(click["goal"], marks), state)
            right = mark_at(marks, click["x"], click["y"])
            r.update(kind="click", image=c["image"], goal=click["goal"], expected=right,
                     correct=r.get("choice") in right)
            clicks += 1
            covered += bool(right)
            clicks_ok += r["correct"]
            rows.append(r)
            print(f"[click] {c['image']}: '{click['goal']}' -> {r.get('choice', r.get('error'))} "
                  f"(want {right or 'none covered'}) {'OK' if r['correct'] else 'MISS'}  {r.get('model_ms', 0):.0f} ms")
        if c.get("page"):
            r = clef.ask(draw_marks(img, [], args.width), page_question(), {"screen": f"{img.width}x{img.height}"})
            r.update(kind="page", image=c["image"], expected=c["page"], correct=r.get("choice") == c["page"])
            pages += 1
            pages_ok += r["correct"]
            rows.append(r)
            print(f"[page]  {c['image']}: {r.get('choice', r.get('error'))} (want {c['page']}) "
                  f"{'OK' if r['correct'] else 'MISS'}  {r.get('model_ms', 0):.0f} ms")

    ms = [r["model_ms"] for r in rows if "model_ms" in r]
    total = [r["total_ms"] for r in rows if "total_ms" in r]
    summary = {
        "backend": f"ollama:{args.ollama_model}" if args.ollama_model else f"local:{args.precision}",
        "image_width": args.width, "marks": args.marks,
        "click_accuracy": f"{clicks_ok}/{clicks}", "targets_covered_by_a_box": f"{covered}/{clicks}",
        "page_accuracy": f"{pages_ok}/{pages}",
        "invalid_or_failed": sum("error" in r for r in rows),
        "model_ms_median": round(statistics.median(ms), 1) if ms else None,
        "model_ms_p90": round(percentile(ms, 0.9), 1), "model_ms_max": round(max(ms, default=0), 1),
        "total_ms_median": round(statistics.median(total), 1) if total else None,
        "max_input_tokens": max((r.get("tokens", 0) for r in rows), default=0),
        "peak_gpu_mb": round(clef.peak_mb()), "load_s": round(clef.load_s, 1),
    }
    (out / "results.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, default=str))
    print("\n" + json.dumps(summary, indent=2))
    print(f"\nFull results: {out / 'results.json'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("env")
    for name in ("marks", "run"):
        p = sub.add_parser(name)
        p.add_argument("cases")
        p.add_argument("--out", default=f"clef-{name}")
        p.add_argument("--marks", choices=["ocr", "grid", "both"], default="both")
        p.add_argument("--grid-cols", type=int, default=8)
        p.add_argument("--grid-rows", type=int, default=5)
        p.add_argument("--max-marks", type=int, default=60, help="cap on OCR boxes per screenshot")
        p.add_argument("--width", type=int, default=896, help="width the marked image is scaled to for Clef")
    run = sub.choices["run"]
    run.add_argument("--precision", choices=["nf4", "fp16", "bf16"], default="nf4")
    run.add_argument("--revision", default=REVISION)
    run.add_argument("--code-sha", default=CODE_SHA)
    run.add_argument("--max-tokens", type=int, default=4096)
    run.add_argument("--warmup", type=int, default=2)
    run.add_argument("--ollama-model", help="use Ollama with this model tag instead of the local checkpoint")
    run.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    args = ap.parse_args()
    {"env": cmd_env, "marks": cmd_marks, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
