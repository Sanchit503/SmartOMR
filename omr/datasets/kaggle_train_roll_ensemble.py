"""Run this file in a Kaggle notebook with the private exported training bundle."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import zipfile

import torch


INPUT_ROOT = Path("/kaggle/input")
WORK_ROOT = Path("/kaggle/working")
# Change this directory name for another experiment; existing models are protected.
OUTPUT_DIR = WORK_ROOT / "smartomr_diverse_ensemble_v1"


def find_package(input_root: Path, work_root: Path) -> Path:
    markers = list(input_root.rglob("package_manifest.json"))
    markers = [path for path in markers if json.loads(path.read_text()).get("kind") == "smartomr_private_training_bundle"]
    if len(markers) == 1:
        return markers[0].parent
    if len(markers) > 1:
        raise RuntimeError("Multiple SmartOMR bundles attached; keep only the intended version")
    archives = list(input_root.rglob("smartomr_kaggle_training.zip"))
    if len(archives) != 1:
        raise FileNotFoundError("Attach the exported private SmartOMR training dataset to this notebook")
    destination = work_root / "smartomr_training_inputs"
    if destination.exists():
        raise FileExistsError("Extraction directory already exists; inspect it instead of overwriting")
    with zipfile.ZipFile(archives[0]) as archive:
        for member in archive.infolist():
            if not (destination / member.filename).resolve().is_relative_to(destination.resolve()):
                raise ValueError("Unsafe archive path")
        archive.extractall(destination)
    return destination


def main() -> None:
    if not torch.cuda.is_available():
        raise RuntimeError("Enable a GPU in Kaggle notebook Settings > Accelerator, then restart the session")
    package = find_package(INPUT_ROOT, WORK_ROOT)
    metadata = json.loads((package / "package_manifest.json").read_text())
    print("GPU:", torch.cuda.get_device_name(0), flush=True)
    print("PyTorch:", torch.__version__, flush=True)
    print("Dataset:", metadata["counts"], flush=True)
    print("Existing Kaggle-pretrained backbones are reused; MNIST pretraining is not repeated.", flush=True)
    command = [
        sys.executable, "-u", "-m", "omr.datasets.train_roll_digit_ensemble",
        "--dataset-dir", str(package / "dataset"),
        "--initial-ensemble", str(package / "models" / "ensemble_manifest.json"),
        "--member-config", str(package / "omr" / "datasets" / "configs" / "roll_digit_ensemble_diverse_v1.json"),
        "--output-dir", str(OUTPUT_DIR),
        "--batch-size", "64", "--device", "cuda",
    ]
    if OUTPUT_DIR.exists():
        raise FileExistsError("Output directory already exists; choose a new OUTPUT_DIR")
    OUTPUT_DIR.mkdir(parents=True)
    # Training stdout is both visible in the notebook and preserved in the output.
    with (OUTPUT_DIR / "training.log").open("w", encoding="utf-8") as log:
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
        raise RuntimeError(f"Training failed with exit code {code}; inspect training.log. Partial files are preserved.")
    manifest = json.loads((OUTPUT_DIR / "ensemble_manifest.json").read_text())
    if manifest["member_count"] != 7:
        raise RuntimeError("Incomplete ensemble; do not deploy it")
    # Includes only output checkpoints/logs/metrics, not the input crop dataset.
    output_zip = shutil.make_archive(str(WORK_ROOT / "smartomr_trained_ensemble"), "zip", OUTPUT_DIR)
    print("Completed ensemble:", output_zip, flush=True)
    print("Compare on unseen writers before replacing the currently selected local ensemble.", flush=True)


if __name__ == "__main__":
    main()
