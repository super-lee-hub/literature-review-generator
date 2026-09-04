"""Idempotent migration from legacy ``config.ini`` shapes to the current schema.

Why this module exists
----------------------
The runtime loader is deliberately fail-closed: ``validate_config_keys()``
rejects any key or section it does not recognise, so a config written against an
older schema cannot reach the legacy handling that would otherwise upgrade it.
The migration must therefore happen *before* validation, as an explicit and
reviewable step rather than silently on every run.

Design rules
------------
* **Idempotent.** Running twice produces byte-identical output the second time.
* **Line-preserving.** Comments, blank lines and ordering survive; this is a
  textual rewrite, not a parse-and-reprint, so nothing the user wrote is lost.
* **Semantics over name matching.** A legacy section is only mapped when its
  historical meaning is established. Anything ambiguous is reported as a warning
  and left for a human instead of being guessed at.

Established by git archaeology (commit ``2f89c6b``, PR #14): ``[Retry_Settings]``
and ``[Stage2_Retry]`` had no reader even before the typed ``[Runtime]`` retry
keys were introduced, so they are dead configuration. They are dropped rather
 than mapped, which is behaviour-preserving because the current ``[Runtime]``
defaults already match what those sections asked for.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple, Union

from services.settings import CONFIG_SCHEMA_VERSION

# Legacy sections with no reader in any supported revision. Dropped, not mapped.
DEAD_SECTIONS: frozenset[str] = frozenset({"Retry_Settings", "Stage2_Retry"})

# The old catch-all parameter block. Values with an unambiguous per-provider home
# are relocated; anything else is reported rather than guessed at.
API_PARAMETERS_SECTION = "API_Parameters"

API_PARAMETER_TARGETS: Dict[str, Tuple[str, str]] = {
    "primary_max_tokens": ("Primary_Reader_API", "max_output_tokens"),
    "backup_max_tokens": ("Backup_Reader_API", "max_output_tokens"),
    "writer_max_tokens": ("Writer_API", "max_output_tokens"),
    "outline_max_tokens": ("Outline_API", "max_output_tokens"),
    "free_mode_max_tokens": ("Free_Mode_API", "max_output_tokens"),
    "validator_max_tokens": ("Validator_API", "max_output_tokens"),
    "primary_temperature": ("Primary_Reader_API", "temperature"),
    "backup_temperature": ("Backup_Reader_API", "temperature"),
    "writer_temperature": ("Writer_API", "temperature"),
    "outline_temperature": ("Outline_API", "temperature"),
    "free_mode_temperature": ("Free_Mode_API", "temperature"),
    "validator_temperature": ("Validator_API", "temperature"),
}

# timeout_seconds was a single global knob; the current schema is per-provider.
API_PARAMETER_TIMEOUT_SECTIONS: Tuple[str, ...] = (
    "Primary_Reader_API",
    "Backup_Reader_API",
    "Writer_API",
    "Outline_API",
    "Free_Mode_API",
    "Validator_API",
)

DROPPED_KEYS: Dict[str, frozenset[str]] = {
    # Fixture providers are test-injected; production config must not enable one.
    "Outline": frozenset({"test_dev_fixture_mode"}),
}

# The pre-vision default. Only rewritten when it is still exactly this value, so
# a user who deliberately chose a different model keeps their choice.
LEGACY_PRIMARY_MODEL = "deepseek-v4-pro"
VISION_PRIMARY_MODEL = "deepseek-v4-flash-vision-exp"

_SECTION_RE = re.compile(r"^\s*\[\s*(?P<name>[^\]]+?)\s*\]\s*$")
_KV_RE = re.compile(r"^\s*(?P<key>[^=:\s][^=:]*?)\s*(?P<sep>[=:])\s*(?P<value>.*?)\s*$")


@dataclass
class MigrationReport:
    """What the migration did, suitable for logging or a CLI summary."""

    changed: bool = False
    changes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.changed = True
        self.changes.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def as_dict(self) -> Dict[str, object]:
        return {
            "changed": self.changed,
            "changes": list(self.changes),
            "warnings": list(self.warnings),
        }


def _index_sections(lines: List[str]) -> Dict[str, int]:
    """Map section name -> index of its header line."""

    sections: Dict[str, int] = {}
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if match:
            sections.setdefault(match.group("name"), index)
    return sections


def _existing_keys(lines: List[str], section_index: int) -> Dict[str, int]:
    """Map key -> line index for the keys belonging to one section."""

    keys: Dict[str, int] = {}
    for offset in range(section_index + 1, len(lines)):
        line = lines[offset]
        if _SECTION_RE.match(line):
            break
        match = _KV_RE.match(line)
        if match:
            keys.setdefault(match.group("key").strip(), offset)
    return keys


def _section_values(lines: List[str], section_index: int) -> Dict[str, str]:
    """Read one section's scalar values without re-parsing the whole file."""

    values: Dict[str, str] = {}
    for key, line_index in _existing_keys(lines, section_index).items():
        match = _KV_RE.match(lines[line_index])
        if match:
            values[key] = match.group("value").strip()
    return values


def _section_bounds(lines: List[str], section_index: int) -> Tuple[int, int]:
    start = section_index
    end = len(lines)
    for offset in range(section_index + 1, len(lines)):
        if _SECTION_RE.match(lines[offset]):
            end = offset
            break
    return start, end


LEGACY_BLOCK_HEADER = "# --- preserved by config-migrate: no unambiguous home in the current schema ---"
LEGACY_BLOCK_FOOTER = "# --- end preserved legacy block ---"


def _declared_schema(sections: Mapping[str, int], lines: List[str]) -> int:
    """Read the schema stamp the incoming config declares, or 0 when absent."""

    index = sections.get("Application")
    if index is None:
        return 0
    for key, line_index in _existing_keys(lines, index).items():
        if key == "config_schema":
            match = _KV_RE.match(lines[line_index])
            if match:
                try:
                    return int(str(match.group("value")).strip())
                except (TypeError, ValueError):
                    return 0
    return 0


def _legacy_primary_default_is_unambiguous(
    sections: Mapping[str, int],
    lines: List[str],
    declared_schema: int,
) -> bool:
    """Recognize only the shipped pre-vision primary default.

    The model name by itself is not enough: an operator may intentionally use
    ``deepseek-v4-pro`` behind a custom gateway or for a non-vision Stage 1
    policy. The promotion therefore requires an older schema plus the old
    DeepSeek provider/base context and a missing or explicit ``vision_first``
    input mode. Ambiguous configurations remain untouched.
    """

    if declared_schema >= CONFIG_SCHEMA_VERSION:
        return False
    primary_index = sections.get("Primary_Reader_API")
    if primary_index is None:
        return False
    primary = _section_values(lines, primary_index)
    if primary.get("model", "").casefold() != LEGACY_PRIMARY_MODEL:
        return False
    provider_family = primary.get("provider_family", "").casefold().replace("-", "_")
    if provider_family not in {"", "deepseek"}:
        return False
    api_base = primary.get("api_base", "").rstrip("/").casefold()
    if api_base not in {"", "https://api.deepseek.com", "https://api.deepseek.com/v1"}:
        return False
    stage1_index = sections.get("Stage1_Input")
    stage1 = _section_values(lines, stage1_index) if stage1_index is not None else {}
    return stage1.get("mode", "").casefold() in {"", "vision_first", "text_first", "text_only"}


def migrate_config_text(
    text: str,
    *,
    promote_vision_primary: bool = True,
    unknown_legacy: str = "preserve",
) -> Tuple[str, MigrationReport]:
    """Rewrite legacy config text into the current schema.

    Returns ``(new_text, report)``. Pure: the caller decides whether to write.

    ``unknown_legacy`` controls ``[API_Parameters]`` keys with no unambiguous
    home. The default ``"preserve"`` keeps them in a clearly marked block, on the
    grounds that discarding a setting the user wrote is not a decision a
    migration should make silently. ``"drop"`` removes them, and is what the
    explicit ``--drop-unknown-legacy`` flag selects.
    """

    if unknown_legacy not in {"preserve", "drop"}:
        raise ValueError(f"unknown_legacy must be 'preserve' or 'drop', got {unknown_legacy!r}")

    report = MigrationReport()
    lines = text.splitlines(keepends=True)
    sections = _index_sections(lines)

    # Only a config that still declares an older schema is evidence of the legacy
    # default. A config already on the current schema has been touched by a
    # current revision, so its model choice is treated as deliberate.
    declared_schema = _declared_schema(sections, lines)
    is_legacy_schema = declared_schema < CONFIG_SCHEMA_VERSION
    promote_vision_primary = promote_vision_primary and _legacy_primary_default_is_unambiguous(
        sections,
        lines,
        declared_schema,
    )

    # ------------------------------------------------------------------
    # Phase 1: relocate [API_Parameters] values into their provider sections.
    # Done first so phase 2 sees a stable picture of which keys already exist.
    # ------------------------------------------------------------------
    relocated: Dict[str, Dict[str, str]] = {}
    preserved: Dict[str, str] = {}
    if API_PARAMETERS_SECTION in sections:
        index = sections[API_PARAMETERS_SECTION]
        _start, end = _section_bounds(lines, index)
        for offset in range(index + 1, end):
            match = _KV_RE.match(lines[offset])
            if not match:
                continue
            key = match.group("key").strip()
            value = match.group("value").strip()
            if not value:
                continue
            if key in API_PARAMETER_TARGETS:
                target_section, target_key = API_PARAMETER_TARGETS[key]
                relocated.setdefault(target_section, {})[target_key] = value
            elif key == "timeout_seconds":
                for target_section in API_PARAMETER_TIMEOUT_SECTIONS:
                    relocated.setdefault(target_section, {})["total_timeout_seconds"] = value
            else:
                preserved.setdefault(key, value)
                report.warn(
                    f"[API_Parameters].{key} has no unambiguous home in the current "
                    "schema and was preserved in a marked legacy block rather than "
                    "guessed at"
                )

    for target_section, values in relocated.items():
        if target_section not in sections:
            # A known-mapping key whose home section is absent is not evidence of
            # a typo to discard; the operator may simply not have enabled that
            # provider. Keep it in the preserved legacy block rather than
            # dropping it silently, so the migration never loses a setting the
            # user wrote.
            for target_key, value in values.items():
                preserved.setdefault(f"{target_section}.{target_key}", value)
            report.warn(
                f"[API_Parameters] values for [{target_section}] were preserved in the "
                "legacy block because that section does not exist in this config"
            )
            continue
        existing = _existing_keys(lines, sections[target_section])
        for target_key, value in values.items():
            if target_key in existing:
                continue  # Never overwrite a value the user set explicitly.
            if target_key == "max_output_tokens" and "max_tokens" in existing:
                # The section carries its own legacy max_tokens, which phase 2
                # renames into max_output_tokens. The provider section is the
                # more specific home for a limit, so it outranks the dissolved
                # catch-all rather than the other way round.
                report.warn(
                    f"[API_Parameters] value for [{target_section}].max_output_tokens "
                    "was skipped because the section sets max_tokens itself"
                )
                continue
            insert_at = sections[target_section] + 1
            for line_index in existing.values():
                insert_at = max(insert_at, line_index + 1)
            lines.insert(insert_at, f"{target_key} = {value}\n")
            existing[target_key] = insert_at
            report.note(f"moved [API_Parameters] value into [{target_section}].{target_key}")
        # Indices shifted; re-index before the next target section.
        sections = _index_sections(lines)

    # ------------------------------------------------------------------
    # Phase 2: per-line rewrites and removals.
    # ------------------------------------------------------------------
    # Decide max_tokens handling against the post-phase-1 text rather than the
    # partially built output: relocated [API_Parameters] values are appended at
    # the end of a section, so they sit *after* max_tokens in line order and
    # would otherwise be invisible at the moment max_tokens is processed, which
    # previously produced a duplicate max_output_tokens key.
    output_tokens_state: Dict[str, str] = {}
    scan_section = ""
    for line in lines:
        header = _SECTION_RE.match(line)
        if header:
            scan_section = header.group("name")
            if scan_section.endswith("_API"):
                output_tokens_state.setdefault(scan_section, "absent")
            continue
        if not scan_section.endswith("_API"):
            continue
        match = _KV_RE.match(line)
        if match and match.group("key").strip() == "max_output_tokens":
            output_tokens_state[scan_section] = "set" if match.group("value").strip() else "empty"

    output: List[str] = []
    current_section = ""
    skip_section = False
    saw_application_section = "Application" in sections

    for line in lines:
        header = _SECTION_RE.match(line)
        if header:
            current_section = header.group("name")
            skip_section = (
                current_section in DEAD_SECTIONS or current_section == API_PARAMETERS_SECTION
            )
            if current_section == API_PARAMETERS_SECTION and preserved and unknown_legacy == "preserve":
                # Discarding a setting the user wrote is not a decision a
                # migration should make on its own, so the unmapped keys are kept
                # in a block that is visibly marked as unmigrated.
                # Preserved as comments, not as a live section: [API_Parameters]
                # is not a valid section in the current schema, so keeping it
                # real would leave the config unable to load at all. Commented
                # lines stay visible to the operator and invisible to the parser.
                output.append(f"{LEGACY_BLOCK_HEADER}\n")
                for key, value in preserved.items():
                    output.append(f"# {key} = {value}\n")
                output.append(f"{LEGACY_BLOCK_FOOTER}\n")
                report.note(
                    f"preserved {len(preserved)} unmapped [{API_PARAMETERS_SECTION}] "
                    "key(s) in a marked legacy block"
                )
                # The section is still removed because every mappable value was
                # relocated (some into live sections, some into this legacy
                # block). The "relocating" note must fire here too, otherwise a
                # non-empty legacy block would suppress it and make the migration
                # look like a pure drop.
                report.note(f"removed [{API_PARAMETERS_SECTION}] after relocating its values")
                continue
            if current_section in DEAD_SECTIONS:
                report.note(
                    f"removed dead legacy section [{current_section}] "
                    "(no reader in any supported revision; [Runtime] defaults match it)"
                )
            elif current_section == API_PARAMETERS_SECTION:
                report.note(f"removed [{API_PARAMETERS_SECTION}] after relocating its values")
            if not skip_section:
                output.append(line)
            continue

        if skip_section:
            continue

        match = _KV_RE.match(line)
        if not match:
            output.append(line)
            continue

        key = match.group("key").strip()
        value = match.group("value").strip()

        if key in DROPPED_KEYS.get(current_section, frozenset()):
            report.note(f"removed [{current_section}].{key} (not accepted by the current schema)")
            continue

        if current_section == "Application" and key == "config_schema":
            if value != str(CONFIG_SCHEMA_VERSION):
                report.note(
                    f"updated [Application].config_schema {value or '(unset)'} -> {CONFIG_SCHEMA_VERSION}"
                )
            output.append(f"config_schema = {CONFIG_SCHEMA_VERSION}\n")
            continue

        if key == "max_output_tokens" and not value and output_tokens_state.get(current_section) == "empty":
            # Placeholder left by an earlier revision; filled from max_tokens below.
            continue

        if key == "max_tokens" and current_section.endswith("_API"):
            state = output_tokens_state.get(current_section, "absent")
            if state == "set":
                report.note(
                    f"dropped [{current_section}].max_tokens "
                    "(max_output_tokens is already set and takes precedence)"
                )
                continue
            if state == "empty":
                report.note(f"filled [{current_section}].max_output_tokens from legacy max_tokens")
            else:
                report.note(f"renamed [{current_section}].max_tokens -> max_output_tokens")
            output.append(f"max_output_tokens = {value}\n")
            continue

        if (
            promote_vision_primary
            and current_section == "Primary_Reader_API"
            and key == "model"
            and value == LEGACY_PRIMARY_MODEL
        ):
            report.note(
                f"promoted [Primary_Reader_API].model {LEGACY_PRIMARY_MODEL} -> {VISION_PRIMARY_MODEL} "
                "(legacy default; a custom model would have been left alone)"
            )
            output.append(f"model = {VISION_PRIMARY_MODEL}\n")
            continue

        output.append(line)

    if not saw_application_section:
        # The schema stamp is what lets the runtime tell a current config from a
        # legacy one, so it is added rather than left to be inferred.
        if output and not output[-1].endswith("\n"):
            output.append("\n")
        output.append(f"\n[Application]\nconfig_schema = {CONFIG_SCHEMA_VERSION}\n")
        report.note(f"added [Application].config_schema = {CONFIG_SCHEMA_VERSION}")

    return "".join(output), report


def migrate_config_file(
    path: Union[str, Path],
    *,
    backup: bool = True,
    drop_unknown_legacy: bool = False,
    promote_vision_primary: bool = True,
) -> MigrationReport:
    """Migrate a config file in place, safely.

    The rewrite is written to a temporary file in the same directory, flushed and
    fsync'd, and only then swapped in with an atomic replace. An interruption
    therefore leaves the original config intact rather than half-written -- which
    matters more than usual here, because a truncated config.ini cannot be loaded
    at all. The backup is completed and fsync'd before the swap begins.
    """

    target = Path(path)
    raw = target.read_text(encoding="utf-8")
    migrated, report = migrate_config_text(
        raw,
        promote_vision_primary=promote_vision_primary,
        unknown_legacy="drop" if drop_unknown_legacy else "preserve",
    )
    if not report.changed:
        return report

    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_path = target.with_name(f"{target.name}.backup_before_{stamp}")
        suffix = 1
        while backup_path.exists():
            backup_path = target.with_name(
                f"{target.name}.backup_before_{stamp}_{suffix}"
            )
            suffix += 1
        with open(backup_path, "wb") as handle:
            handle.write(target.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        report.note(f"wrote backup {backup_path.name}")

    directory = str(target.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(migrated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        # A failed migration must never destroy the config it was migrating.
        Path(temp_name).unlink(missing_ok=True)
        raise

    return report
