"""Upload, train and evaluate the private SmartOMR bundle in a Colab GPU notebook."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile

import torch


BUNDLE_PATH = Path("/content/smartomr_colab_training.zip")
PACKAGE_DIR = Path("/content/smartomr_training_inputs")
OUTPUT_DIR = Path("/content/smartomr_colab_ensemble_v1")


def run_logged(command, package, log_path):
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=package, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, bufsize=1)
        try:
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
            code = process.wait()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
    if code:
        raise RuntimeError(f"Command failed with exit code {code}; inspect {log_path.name}")


def main():
    from google.colab import files

    if not torch.cuda.is_available():
        raise RuntimeError("Select Runtime > Change runtime type > GPU, then reconnect")
    if not BUNDLE_PATH.is_file():
        print("Upload smartomr_colab_training.zip. Keep the notebook and data private.")
        files.upload()
    if not BUNDLE_PATH.is_file():
        raise FileNotFoundError("Upload the ZIP using its original filename")
    bundle_hash = hashlib.sha256(BUNDLE_PATH.read_bytes()).hexdigest()
    hash_path = PACKAGE_DIR / ".bundle_sha256"
    if PACKAGE_DIR.exists() and (not hash_path.is_file() or hash_path.read_text() != bundle_hash):
        raise ValueError("A different bundle is already extracted; use a fresh runtime or PACKAGE_DIR")
    if not PACKAGE_DIR.exists():
        with zipfile.ZipFile(BUNDLE_PATH) as archive:
            for member in archive.infolist():
                if not (PACKAGE_DIR / member.filename).resolve().is_relative_to(PACKAGE_DIR.resolve()):
                    raise ValueError("Unsafe archive path")
            archive.extractall(PACKAGE_DIR)
        hash_path.write_text(bundle_hash)
    metadata = json.loads((PACKAGE_DIR / "package_manifest.json").read_text())
    if metadata.get("kind") != "smartomr_private_training_bundle" or metadata.get("checkpoint_stage") != "kaggle":
        raise ValueError("Use the Colab bundle with Kaggle-only starting checkpoints for the final-test split")
    print("GPU:", torch.cuda.get_device_name(0), "PyTorch:", torch.__version__, flush=True)
    print("Split:", metadata["counts"], flush=True)
    report_path = PACKAGE_DIR / "split_report.json"
    split_report = json.loads(report_path.read_text()) if report_path.is_file() else {
        "warning": "Small validation split; checkpoint ranking may be unstable." if metadata["counts"]["val"] < 200 else None,
        "class_counts": metadata["class_counts"],
    }
    if split_report.get("warning"):
        print("WARNING:", split_report["warning"], flush=True)
    missing = sorted(set(str(digit) for digit in range(10)) - set(split_report["class_counts"]["val"]))
    if missing:
        print("WARNING: Validation has no examples of digits", missing, flush=True)
    print("Kaggle-only pretrained weights; test crops are not used for fitting or model selection.", flush=True)
    if OUTPUT_DIR.exists():
        raise FileExistsError("Choose a new OUTPUT_DIR; previous checkpoints are preserved")
    OUTPUT_DIR.mkdir(parents=True)
    run_logged([
        sys.executable, "-u", "-m", "omr.datasets.train_roll_digit_ensemble",
        "--dataset-dir", str(PACKAGE_DIR / "dataset"),
        "--initial-ensemble", str(PACKAGE_DIR / "models/ensemble_manifest.json"),
        "--member-config", str(PACKAGE_DIR / "omr/datasets/configs/roll_digit_ensemble_diverse_v1.json"),
        "--output-dir", str(OUTPUT_DIR), "--batch-size", "64", "--device", "cuda",
    ], PACKAGE_DIR, OUTPUT_DIR / "training.log")
    manifest = OUTPUT_DIR / "ensemble_manifest.json"
    if json.loads(manifest.read_text())["member_count"] != 7:
        raise RuntimeError("Incomplete ensemble; do not deploy it")
    run_logged([
        sys.executable, "-u", "-m", "omr.datasets.evaluate_exported_holdout",
        "--dataset-dir", str(PACKAGE_DIR / "dataset"), "--ensemble-manifest", str(manifest),
        "--output-dir", str(OUTPUT_DIR / "final_test"), "--device", "cuda",
    ], PACKAGE_DIR, OUTPUT_DIR / "test.log")
    result = shutil.make_archive("/content/smartomr_trained_ensemble", "zip", OUTPUT_DIR)
    print("Completed models and final-test report:", result, flush=True)
    files.download(result)


if __name__ == "__main__":
    main()
