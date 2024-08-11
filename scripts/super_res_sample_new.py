"""
Generate a large batch of samples from a super resolution model, given a batch
of samples from a regular model from image_sample.py.
"""

import argparse
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader
from tqdm import tqdm
import os
from improved_diffusion.mlde_sr import MLDEDataset, MLDESingleDataset
from improved_diffusion.ppt_sr import PPTSRDataset
from improved_diffusion.script_util import (
    add_dict_to_argparser,
    args_to_dict,
    sr_create_model_and_diffusion,
    sr_model_and_diffusion_defaults,
)

# def guidance_loss(sample, high_res, scale=1.0):
#     """Compute the guidance loss as the difference between the sample and low-resolution image."""
#     return scale * torch.nn.functional.mse_loss(sample, high_res)
# def guidance_loss(sample, low_res, scale=1.0):
#     """
#     Compute the guidance loss as the bias between the sample and high-resolution image.
#     The bias is calculated as the mean difference (sample - low_res).
#     """
#     # Calculate bias as the mean of the difference between sample and high-resolution
#     bias = torch.mean(sample - low_res)
#     # Optionally scale the bias if necessary
#     return scale * bias


def guidance_loss(sample, low_res, scale=1.0, alpha=0.5):
    """
    Combine a form of RMSE (relative to low_res) and bias into a single guidance loss.
    """
    # Compute a pseudo-RMSE loss between sample and low_res (not true RMSE but relative)
    pseudo_rmse_loss = torch.sqrt(torch.mean((sample - low_res) ** 2))
    # Compute the bias between sample and low_res
    bias = torch.mean(sample - low_res)
    # Combine the pseudo-RMSE and bias into a single loss
    combined_loss = alpha * pseudo_rmse_loss + (1 - alpha) * torch.abs(bias)
    return scale * combined_loss


def main():
    args = create_argparser().parse_args()

    print("creating model...", args)
    if args.dataset == "prism":
        in_channels, cond_channels = 1, 1
        if len(args.topo_file) > 0:
            cond_channels += 1
    elif args.dataset == "mlde":
        in_channels, cond_channels = 1, 14
    elif args.dataset == "mlde_single":
        in_channels, cond_channels = 1, 1
    else:
        raise Exception(f"Unsupported dataset {args.data_dir}")

    model, diffusion = sr_create_model_and_diffusion(
        in_channels,
        cond_channels,
        **args_to_dict(args, sr_model_and_diffusion_defaults().keys()),
    )
    model.load_state_dict(torch.load(args.model_path))
    model = model.to("cuda")
    model.eval()

    print("creating data loader...")
    if args.dataset == "prism":
        val_ds = PPTSRDataset(
            args.data_dir,
            2021,
            2022,
            args.large_size,
            args.small_size,
            args.norm,
            args.topo_file,
        )
    elif args.dataset == "mlde_single":
        assert args.large_size == 64
        val_ds = MLDESingleDataset(
            Path(args.data_dir) / "test.nc",
            norm=args.norm,
            large_size=args.large_size,
            small_size=args.small_size,
        )
    elif args.dataset == "mlde":
        assert args.large_size == 64
        val_ds = MLDEDataset(Path(args.data_dir) / "train.nc", norm=args.norm)
    else:
        raise Exception(f"Unsupported dataset {args.data_dir}")
    val_loader = DataLoader(
        val_ds, batch_size=args.batch_size, num_workers=16, shuffle=False
    )

    print("creating samples...")
    all_samples = []
    all_hrs = []
    all_lrs = []
    for i, batch in enumerate(tqdm(val_loader)):
        if args.num_samples != -1:
            if i > (args.num_samples // args.batch_size):
                break
        lr = batch["lr"]
        # hr = batch["hr"]  # Extract the high-resolution images from the batch
        
        model_kwargs = {"low_res": lr.to("cuda")}

        sample = diffusion.p_sample_loop(
            model,
            (lr.size()[0], 1, args.large_size, args.large_size),
            clip_denoised=args.clip_denoised,
            model_kwargs=model_kwargs,
        ).requires_grad_(True)  # Enable gradient tracking
        
        # 1. Calculate guidance loss
        loss = guidance_loss(sample, lr.to("cuda"), scale=args.scale, alpha=args.alpha)
        if i % 10 == 0:
            print(i, "loss:", loss.item())
        # 2. Compute gradients for guidance
        grad = torch.autograd.grad(loss, sample)[0]

        # 3. Apply guidance: adjust the sample based on the gradient
        sample = sample - args.guidance_scale * grad
        
        # Detach the sample from the computation graph before converting to numpy
        all_samples.append(sample.detach().cpu().numpy())
        all_hrs.append(batch["hr"].cpu().numpy())
        all_lrs.append(batch["lr"].cpu().numpy())

    hr = np.concatenate(all_hrs, axis=0)
    lr = np.concatenate(all_lrs, axis=0)
    sample = np.concatenate(all_samples, axis=0)
    path = Path(args.model_path).parent
    # Create directory if it doesn't exist
    np.savez(f"{path}/sample_{args.scale}_{args.alpha}.npz", hr=hr, lr=lr, sample=sample)


def create_argparser():
    defaults = dict(
        clip_denoised=True,
        dataset="prism",  # prism, mlde
        num_samples=16,
        batch_size=16,
        use_ddim=False,
        data_dir="",
        model_path="",
        norm="gamma",
        topo_file="",
        guidance_scale=0.1,  # Add guidance_scale with a default value
        scale=1.0,
        alpha=0.5,
    )
    defaults.update(sr_model_and_diffusion_defaults())
    parser = argparse.ArgumentParser()
    add_dict_to_argparser(parser, defaults)
    return parser


if __name__ == "__main__":
    main()
