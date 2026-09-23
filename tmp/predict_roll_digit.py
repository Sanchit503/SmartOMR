import argparse
from pathlib import Path

import numpy as np
import torch
from PIL import Image, ImageOps

from omr.datasets.train_roll_digit_resnet import LABELS, SmallRollDigitResNet


def load_model(path: Path, device: torch.device):
    payload = torch.load(path, map_location=device)
    model = SmallRollDigitResNet().to(device)
    state = payload.get('model_state') or payload.get('model_state_dict') or payload.get('state_dict') or payload
    model.load_state_dict(state)
    model.eval()
    return model


def preprocess(path: Path, image_size: int = 64):
    image = Image.open(path).convert('L')
    image = ImageOps.autocontrast(image)
    image = ImageOps.pad(image, (image_size, image_size), color=255)
    array = np.asarray(image, dtype=np.float32) / 255.0
    array = (1.0 - array - 0.15) / 0.35
    return torch.from_numpy(array).unsqueeze(0).unsqueeze(0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('image', type=Path)
    parser.add_argument('--model', type=Path, default=Path('data/models/roll_digit_resnet.pt'))
    parser.add_argument('--topk', type=int, default=5)
    args = parser.parse_args()
    device = torch.device('cpu')
    model = load_model(args.model, device)
    x = preprocess(args.image).to(device)
    with torch.no_grad():
        probs = torch.softmax(model(x), dim=1)[0].cpu().numpy()
    order = probs.argsort()[::-1][:args.topk]
    print(f'image={args.image}')
    print(f'prediction={LABELS[int(order[0])]} confidence={probs[int(order[0])]:.4f}')
    print('topk=')
    for idx in order:
        print(f'  {LABELS[int(idx)]}: {probs[int(idx)]:.4f}')


if __name__ == '__main__':
    main()
