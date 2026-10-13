"""Minimal external-process demonstration; no CARLA or model import."""
import argparse
from easycarla_client import CarlaEnvClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", default="carla_01")
    parser.add_argument("--episodes", type=int, default=2)
    parser.add_argument("--steps", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="Request timeout in seconds, including large-map reset (default: 180)")
    args = parser.parse_args()
    env = CarlaEnvClient(instance_name=args.instance, timeout=args.timeout)
    try:
        for episode in range(args.episodes):
            obs, info = env.reset(seed=episode)
            print("reset", episode, info.get("scenario", {}))
            for _ in range(args.steps):
                obs, reward, terminated, truncated, info = env.step([0.0, 0.15, 0.0])
                print("step", obs.get("frame"), reward, terminated, truncated)
                if terminated or truncated:
                    break
    finally:
        env.close()


if __name__ == "__main__":
    main()
