"""Split the generated ground truth into a consumable registry and a withheld evaluation set.

Why this exists: `scripts/disambiguate.py` repaired the graph using `data/ground_truth.json` and
`scripts/verify_graph.py` then scored the result against that same file. Fixing something with the
answer key and grading it with the answer key produces a number that is not evidence.

The split makes the supervision legitimate:

* `data/entity_registry.json` — canonical entity registry the **pipeline may consume**. This is the
  analogue of production master data: a system doing entity resolution normally does have a registry
  of the entities it cares about.
* `data/eval_holdout.json` — entities deliberately **withheld** from the pipeline. Nothing in the
  ingestion path may read this file. It exists so evaluation can report separately on entities the
  pipeline was never told about, which is the only way to tell entity resolution from a lookup.

`data/ground_truth.json` stays as the full evaluation reference — evaluation is allowed to see
everything; ingestion is not.

Holdout selection is deterministic (fixed seed) so runs are comparable, and it deliberately withholds
one **complete homonym group**: whichever pair is withheld must be separated by the mechanism itself
rather than by looking it up, while the other pair tests the registry-assisted path.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path
from typing import Any

ENTITY_SECTIONS = ("houses", "megacorporations", "persons", "stations_and_ships")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("split_ground_truth")


def load_ground_truth(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Ground truth not found: {path}. Run scripts/generate_synthetic_data.py --seed-only first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def homonym_groups(gt: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Group stations and ships that share a name — these are the planted homonyms."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in gt.get("stations_and_ships", []):
        key = item["name"].strip().lower()
        groups.setdefault(key, []).append(item)
    return {name: items for name, items in groups.items() if len(items) > 1}


def choose_holdout(gt: dict[str, Any], fraction: float, seed: int) -> set[str]:
    """
    Pick entity ids to withhold: one complete homonym group plus a deterministic sample of the rest.

    Withholding a whole group matters. Splitting a group would leave half of it in the registry, and
    the pipeline could resolve the pair by looking up the half it was given — which is exactly the
    shortcut this split exists to prevent.
    """
    rng = random.Random(seed)
    held: set[str] = set()

    groups = homonym_groups(gt)
    if groups:
        # Deterministic choice: the alphabetically last group, so it does not drift between runs.
        name = sorted(groups)[-1]
        held.update(item["id"] for item in groups[name])
        logger.info(
            "Withholding homonym group %r in full: %s",
            groups[name][0]["name"],
            ", ".join(sorted(held)),
        )

    remaining = [
        entity["id"]
        for section in ENTITY_SECTIONS
        for entity in gt.get(section, [])
        if entity["id"] not in held
    ]
    extra_count = max(0, round(len(remaining) * fraction))
    held.update(rng.sample(sorted(remaining), k=min(extra_count, len(remaining))))
    return held


def build_registry(gt: dict[str, Any], held: set[str]) -> dict[str, Any]:
    """
    Build the pipeline-consumable registry: entities minus the holdout.

    Hierarchy links are filtered too. A link naming a withheld entity would leak its id and its
    owner — the very discriminator the mechanism is supposed to derive on its own. The timeline is
    left out entirely: it is narrative evidence, not master data.
    """
    registry: dict[str, Any] = {"sector_name": gt.get("sector_name")}
    for section in ENTITY_SECTIONS:
        registry[section] = [e for e in gt.get(section, []) if e["id"] not in held]

    registry["hierarchy"] = [
        link
        for link in gt.get("hierarchy", [])
        if link.get("parent_id") not in held and link.get("child_id") not in held
    ]
    return registry


def build_holdout(gt: dict[str, Any], held: set[str]) -> dict[str, Any]:
    """Build the evaluation-only view of the withheld entities."""
    holdout: dict[str, Any] = {
        "sector_name": gt.get("sector_name"),
        "withheld_entity_ids": sorted(held),
    }
    for section in ENTITY_SECTIONS:
        holdout[section] = [e for e in gt.get(section, []) if e["id"] in held]
    return holdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ground-truth", type=Path, default=Path("data/ground_truth.json"))
    parser.add_argument("--registry", type=Path, default=Path("data/entity_registry.json"))
    parser.add_argument("--holdout", type=Path, default=Path("data/eval_holdout.json"))
    parser.add_argument(
        "--fraction",
        type=float,
        default=0.2,
        help="share of non-homonym entities to withhold on top of the homonym group",
    )
    parser.add_argument("--seed", type=int, default=20260806, help="fixed for reproducibility")
    args = parser.parse_args()

    gt = load_ground_truth(args.ground_truth)
    held = choose_holdout(gt, args.fraction, args.seed)

    registry = build_registry(gt, held)
    holdout = build_holdout(gt, held)

    total = sum(len(gt.get(s, [])) for s in ENTITY_SECTIONS)
    kept = sum(len(registry[s]) for s in ENTITY_SECTIONS)
    if kept + len(held) != total:
        logger.error("Entity accounting is off: %d kept + %d held != %d total", kept, len(held), total)
        return 1

    for path, payload in ((args.registry, registry), (args.holdout, holdout)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    dropped_links = len(gt.get("hierarchy", [])) - len(registry["hierarchy"])
    logger.info("Registry: %d entities -> %s", kept, args.registry)
    logger.info("Holdout:  %d entities -> %s", len(held), args.holdout)
    logger.info("Hierarchy links dropped from registry to avoid leaking holdout ids: %d", dropped_links)
    logger.info("Withheld: %s", ", ".join(sorted(held)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
