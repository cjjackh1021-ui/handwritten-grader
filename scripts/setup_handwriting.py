"""Explicit initialization only. Models are never downloaded by a grading request.

python scripts/setup_handwriting.py --components handwriting paddle chandra
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import urllib.parse
import venv

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "workers/components.lock.json").read_text(encoding="utf-8"))


def download(url, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "local-handwriting-grader"})
    with urllib.request.urlopen(request, timeout=120) as response:
        target.write_bytes(response.read())
    return hashlib.sha256(target.read_bytes()).hexdigest()


def verify_file(path, expected):
    with Path(path).open("rb") as stream: digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != expected: raise RuntimeError(f"SHA-256 mismatch: {path}")


def revision(repo, branch):
    with urllib.request.urlopen(urllib.request.Request(f"https://api.github.com/repos/{repo}/commits/{branch}",
                                 headers={"User-Agent": "local-handwriting-grader"}), timeout=30) as response:
        return json.load(response)["sha"]


def setup(component, packages):
    directory = ROOT / f".venv-{component}"
    python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(directory)
    subprocess.run([str(python), "-m", "pip", "install", *packages], check=True)
    locks = ROOT / "workers" / "locks"
    locks.mkdir(exist_ok=True)
    installed = json.loads(subprocess.check_output([str(python), "-m", "pip", "list", "--format=json"], text=True))
    lock = "\n".join(f"{p['name']}=={p['version']}" for p in installed) + "\n"
    if component == "chandra": lock = "--extra-index-url https://download.pytorch.org/whl/cu128\n" + lock
    (locks / f"{component}.txt").write_text(lock, encoding="utf-8")
    return python


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--components", nargs="+", choices=["handwriting", "paddle", "chandra"],
                        default=["handwriting", "paddle", "chandra"])
    args = parser.parse_args()
    for name in args.components:
        print(f"Initializing {name}", flush=True)
        model_dir = ROOT / "models" / name
        model_dir.mkdir(parents=True, exist_ok=True)
        if name == "handwriting":
            setup(name, ["handrightbeta==8.7.0", "Pillow==10.4.0", "fonttools==4.60.1"])
            repo = "14790897/handwriting-web"
            ref = LOCK["handwriting"]["revision"]
            directory = ROOT / "assets" / "handwriting"
            files = []
            for font in ("李国夫手写体.ttf", "神韵英子楷书.ttf"):
                url = f"https://raw.githubusercontent.com/{repo}/{ref}/ttf_files/" + urllib.parse.quote(font)
                digest = download(url, directory / font)
                verify_file(directory / font, LOCK["handwriting"]["sha256"][font])
                files.append({"file": font, "sha256": digest, "source": url})
            download(f"https://raw.githubusercontent.com/{repo}/{ref}/LICENSE", directory / "UPSTREAM_LICENSE.txt")
            download("https://raw.githubusercontent.com/14790897/Handright/master/LICENSE.txt", directory / "HANDRIGHT_LICENSE.txt")
            (directory / "manifest.json").write_text(json.dumps({"repository": repo, "revision": ref,
                "renderer": "handrightbeta==8.7.0", "fonts": files,
                "notice": "Repository code license does not automatically grant a separate font license. Preserve upstream font attribution."},
                ensure_ascii=False, indent=2), encoding="utf-8")
        elif name == "paddle":
            python = setup(name, LOCK["paddle"]["packages"])
            code = """
from paddleocr import PaddleOCR
from pathlib import Path
import shutil, sys
ocr = PaddleOCR(device='cpu', text_detection_model_name='PP-OCRv6_medium_det', text_recognition_model_name='PP-OCRv6_medium_rec', use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False)
target=Path(sys.argv[1])
for kind, name in [('det','PP-OCRv6_medium_det'),('rec','PP-OCRv6_medium_rec')]:
    source=Path.home()/'.paddlex'/'official_models'/name
    if not source.is_dir(): raise RuntimeError('Model download not found: '+str(source))
    shutil.copytree(source,target/kind,dirs_exist_ok=True)
"""
            subprocess.run([str(python), "-c", code, str(model_dir)], check=True)
            for filename, digest in LOCK["paddle"]["sha256"].items(): verify_file(model_dir / filename, digest)
        else:
            if os.name == "nt" and sys.version_info[:2] == (3,12):
                from download_asset import fetch
                wheel = LOCK["torch_windows_cp312"]
                target = ROOT / "models/downloads" / wheel["file"]
                if not target.exists(): fetch(wheel["url"], target, wheel["sha256"])
                verify_file(target, wheel["sha256"])
                torch_package = str(target)
            else:
                torch_package = "torch==2.11.0"
            python = setup(name, [torch_package, "torchvision==0.26.0", "--index-url", "https://download.pytorch.org/whl/cu128"])
            python = setup(name, ["chandra-ocr[hf]==0.2.0", "transformers==5.20.1", "huggingface-hub==2.2.0"])
            code = """
from huggingface_hub import snapshot_download
import json, sys
from pathlib import Path
repo='datalab-to/chandra-ocr-2'
ref=sys.argv[2]
snapshot_download(repo,revision=ref,local_dir=Path(sys.argv[1])/'weights',allow_patterns=['*.json','*.jinja','LICENSE','README.md'])
(Path(sys.argv[1])/'revision.json').write_text(json.dumps({'repository':repo,'revision':ref}))
"""
            ref = LOCK["chandra"]["revision"]
            subprocess.run([str(python), "-c", code, str(model_dir), ref], check=True)
            from download_asset import fetch
            weights = model_dir / "weights/model.safetensors"
            if not weights.exists():
                fetch(f"https://huggingface.co/datalab-to/chandra-ocr-2/resolve/{ref}/model.safetensors", weights, LOCK["chandra"]["weights_sha256"])
            verify_file(weights, LOCK["chandra"]["weights_sha256"])
            ref = LOCK["chandra"]["code_revision"]
            download(f"https://raw.githubusercontent.com/datalab-to/chandra/{ref}/MODEL_LICENSE", model_dir / "MODEL_LICENSE")
        if name != "handwriting":
            hashes = {}
            for path in sorted(model_dir.rglob("*")):
                if path.is_file() and '.cache' not in path.parts and path.name != 'ready.json':
                    digest = hashlib.sha256()
                    with path.open('rb') as stream:
                        for chunk in iter(lambda: stream.read(8*1024*1024), b''): digest.update(chunk)
                    hashes[str(path.relative_to(model_dir))] = digest.hexdigest()
            (model_dir / "ready.json").write_text(json.dumps({"engine": name, "sha256": hashes}, indent=2), encoding="utf-8")
        print(f"Ready: {name}", flush=True)


if __name__ == "__main__":
    main()
