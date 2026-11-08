"""Registry of device profiles: built-in ones, the user's own, and a safe generic fallback.

Profiles are JSON files. They are read from, in increasing priority:

    realmebuds/profiles/builtin/*.json           shipped with the project
    ~/.config/realme-buds/profiles/*.json        the user's own (XDG_CONFIG_HOME respected)
    $REALME_BUDS_PROFILE_DIR/*.json              for testing and one-off use

A later profile with the same ``key`` replaces an earlier one, so a user can correct a
built-in profile without touching the project.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .schema import (  # noqa: F401  (re-exported)
    EVIDENCE_LEVELS,
    FEATURE_GROUPS,
    KNOWN_FAMILIES,
    RESERVED_FEATURE_KEYS,
    AncGroup,
    AncMode,
    CustomEq,
    DeviceProfile,
    EqPreset,
    Feature,
    HoldOption,
    ProfileError,
    profile_from_dict,
)

BUILTIN_DIR = Path(__file__).parent / "builtin"
DEFAULT_KEY = "realme-buds-air7-pro"


def user_profile_dirs() -> list[Path]:
    dirs = []
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    dirs.append(base / "realme-buds" / "profiles")
    extra = os.environ.get("REALME_BUDS_PROFILE_DIR")
    if extra:
        dirs.append(Path(extra))
    return dirs


def load_file(path: Path) -> DeviceProfile:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ProfileError(f"{path}: not valid JSON ({e})") from e
    profile = profile_from_dict(data, source=str(path))
    return profile if path.parent != BUILTIN_DIR else _with_source(profile, "builtin")


def _with_source(profile: DeviceProfile, source: str) -> DeviceProfile:
    from dataclasses import replace

    return replace(profile, source=source)


def load_all(extra_dirs: list[Path] | None = None, errors: list[str] | None = None) -> dict[str, DeviceProfile]:
    """Every profile by key. A broken user profile is skipped and described in ``errors``;
    a broken built-in one is a bug and raises."""
    found: dict[str, DeviceProfile] = {}
    for path in sorted(BUILTIN_DIR.glob("*.json")):
        p = load_file(path)
        found[p.key] = p
    for d in [*user_profile_dirs(), *(extra_dirs or [])]:
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.json")):
            try:
                p = load_file(path)
            except ProfileError as e:
                if errors is not None:
                    errors.append(str(e))
                continue
            found[p.key] = p
    return found


def get(key: str, profiles: dict[str, DeviceProfile] | None = None) -> DeviceProfile:
    profiles = profiles or load_all()
    if key not in profiles:
        raise ProfileError(f"no profile {key!r}; known: {', '.join(sorted(profiles)) or 'none'}")
    return profiles[key]


def find(product_id: str | None, name: str | None, profiles: dict[str, DeviceProfile] | None = None) -> DeviceProfile | None:
    """The profile for these buds. A product id match beats a name match."""
    profiles = profiles or load_all()
    if product_id:
        pid = product_id.upper()
        for p in profiles.values():
            if pid in p.product_ids:
                return p
    if name:
        low = name.strip().lower()
        for p in profiles.values():
            if any(n.lower() == low for n in p.names):
                return p
    return None


def generic_profile(product_id: str | None = None, name: str | None = None) -> DeviceProfile:
    """What we use for buds we have no profile for.

    It claims nothing: no noise-control table (a wrong one can turn ANC on when asked to
    turn it off on some models), no gesture options, no EQ presets. Battery, wearing state,
    firmware and the feature switches the buds themselves report still work. The user can pick
    a sibling model's profile (``--profile``) as an explicit override.
    """
    label = name or "Unknown earbuds"
    return DeviceProfile(
        key="generic",
        name=label,
        vendor="unknown",
        product_ids=(product_id,) if product_id else (),
        notes="No profile for these buds. See docs/ADDING_A_DEVICE.md or run: realme-buds profile detect",
        source="generic",
        generic=True,
    )


def resolve(
    product_id: str | None,
    name: str | None,
    override: str | None = None,
    profiles: dict[str, DeviceProfile] | None = None,
) -> DeviceProfile:
    """Pick the profile: an explicit override, else a match by product id or name, else generic."""
    profiles = profiles or load_all()
    if override:
        return get(override, profiles)
    return find(product_id, name, profiles) or generic_profile(product_id, name)


def known_feature_keys(profiles: dict[str, DeviceProfile] | None = None) -> dict[str, Feature]:
    """Union of every profile's feature keys, for building command names."""
    out: dict[str, Feature] = {}
    for p in (profiles or load_all()).values():
        for f in p.features:
            out.setdefault(f.key, f)
    return out
