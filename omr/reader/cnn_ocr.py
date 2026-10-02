
from __future__ import annotations
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import transforms
from PIL import Image
import numpy as np
from pathlib import Path

from omr.reader.handwriting import RollOcrBackend, RollOcrResult, normalize_handwritten_roll_text
from omr.reader.digit_model import extract_digit_feature
from omr.grading.bubbles import _expected_digit_count

class CnnDigitModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 32, kernel_size=3, padding=1)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=3, padding=1)
        self.pool = nn.MaxPool2d(2, 2)
        self.fc1 = nn.Linear(64 * 7 * 7, 128)
        self.fc2 = nn.Linear(128, 10)
        self.dropout = nn.Dropout(0.5)

    def forward(self, x):
        x = self.pool(F.relu(self.conv1(x)))
        x = self.pool(F.relu(self.conv2(x)))
        x = x.view(-1, 64 * 7 * 7)
        x = F.relu(self.fc1(x))
        x = self.dropout(x)
        x = self.fc2(x)
        return x

class PyTorchCnnRollOcr:
    provider = "pytorch_cnn"
    fast_cell_first = True

    def __init__(self, model_path: str | Path):
        self.device = torch.device("cpu")
        self.model = CnnDigitModel().to(self.device)
        self.model.load_state_dict(torch.load(model_path, map_location=self.device))
        self.model.eval()
        self.transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.5,), (0.5,))
        ])

    def read_digit(self, crop_path: Path) -> RollOcrResult:
        image = Image.open(crop_path)
        feat = extract_digit_feature(image)
        if feat.is_blank:
            return RollOcrResult("", confidence=0.0, raw={"reason": "blank"})

        norm_img = Image.fromarray(feat.normalized_image, mode="L")
        tensor = self.transform(norm_img).unsqueeze(0).to(self.device)

        with torch.no_grad():
            output = self.model(tensor)
            probs = F.softmax(output, dim=1).squeeze(0)
            conf, pred = torch.max(probs, 0)

        digit = str(pred.item())
        confidence = conf.item()
        return RollOcrResult(digit, confidence=confidence, raw={"probs": probs.tolist(), "provider": self.provider})

    def read_roll_cells(self, cell_paths: list[Path], program: str | None = None, valid_rolls: set[str] | None = None) -> RollOcrResult:
        all_probs = []
        digits = ""
        joint_prob_unconstrained = 1.0

        for path in cell_paths:
            res = self.read_digit(path)
            raw = res.raw or {}
            if "probs" in raw:
                probs = raw["probs"]
                digits += res.text
                joint_prob_unconstrained *= res.confidence
            else:
                probs = [0.1] * 10
                digits += "?"
                joint_prob_unconstrained *= 0.0
            all_probs.append(probs)

        if "?" in digits:
            return RollOcrResult(digits, confidence=0.0, raw={"reason": "unreadable_cells", "provider": self.provider})

        # Roster-constrained decoding
        if valid_rolls:
            best_roll = None
            best_prob = -1.0
            for roster_roll in valid_rolls:
                # filter out non-matching programs if program is known
                if program == "MTECH" and not roster_roll.startswith("MT"): continue
                if program == "PHD" and not roster_roll.startswith("PHD"): continue
                if program == "BTECH" and (roster_roll.startswith("MT") or roster_roll.startswith("PHD")): continue

                digits_only = "".join(c for c in roster_roll if c.isdigit())
                if len(digits_only) != len(all_probs):
                    continue

                prob = 1.0
                for i, char in enumerate(digits_only):
                    prob *= all_probs[i][int(char)]

                if prob > best_prob:
                    best_prob = prob
                    best_roll = digits_only

            if best_roll and best_prob > 1e-5: # threshold
                if best_roll != digits:
                    confidence = min(0.6, best_prob ** (1/len(all_probs))) # lower confidence if it was corrected
                else:
                    confidence = best_prob ** (1/len(all_probs))
                return RollOcrResult(best_roll, confidence=confidence, raw={"provider": self.provider, "constrained": True})

        confidence = joint_prob_unconstrained ** (1/len(all_probs)) if all_probs else 0.0
        return RollOcrResult(digits, confidence=confidence, raw={"provider": self.provider})

    def read_roll(self, crop_path: Path, program: str | None = None, valid_rolls: set[str] | None = None) -> RollOcrResult:
        return RollOcrResult("", confidence=0.0, raw={"reason": "cnn_is_cell_only"})
