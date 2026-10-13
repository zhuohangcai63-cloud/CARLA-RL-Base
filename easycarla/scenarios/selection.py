"""Episode-level source selection with explicit failure policy."""
from copy import deepcopy
import random


def select_source(config, seed, options):
    config = deepcopy(config)
    source = config.get("source", "scenic")
    if source == "mixed":
        entries = config.get("sources", [])
        if not entries:
            raise ValueError("mixed requires explicit sources and weights")
        chosen = random.Random(seed).choices(entries, weights=[s.get("weight", 1) for s in entries], k=1)[0]
        config = deepcopy(chosen)
        source = config["source"]
    if "source" in options and options["source"] != source:
        raise ValueError("reset cannot override configured scenario source")
    if source == "scenic":
        files = config.get("files")
        if files:
            from pathlib import Path
            wanted = options.get("scenario_id")
            matches = [p for p in files if Path(p).stem == wanted] if wanted else list(files)
            if not matches:
                raise ValueError("scenario_id is not in the configured Scenic manifest")
            config["file"] = random.Random(seed).choice(matches)
        from .scenic_source import ScenicAdapter
        return ScenicAdapter(config), config
    if source == "bench2drive":
        from .bench2drive import Bench2DriveAdapter
        return Bench2DriveAdapter(config), config
    raise ValueError(f"Unsupported source {source!r}")
