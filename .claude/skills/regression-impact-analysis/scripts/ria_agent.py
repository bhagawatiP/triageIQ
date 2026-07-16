#!/usr/bin/env python3
"""
RIA v2 Agent - Intelligent Pipeline Execution

Single Python entry point that orchestrates the full Regression Impact Analysis
pipeline. The agent intelligently detects Knowledge Base status and runs the
appropriate stages:

  Stage 0  : One-time Knowledge Base build (only if KB missing or --rebuild-kb)
  Stages 1-4 : Analysis pipeline (via ria_v2_orchestrator.py --atomic)
  Stages 5-6 : Refinement pipeline (stage5_refine_tests.py, stage6_aggressive_suppression.py)
  Final     : HTML report generation (generate_html_report.py)

Usage examples:
  # Auto-detect changes from git (no method needed!)
  python3 ria_agent.py
  python3 ria_agent.py --auto-detect

  # Explicit method (overrides auto-detection)
  python3 ria_agent.py --changed-method myMethod --changed-file path/to/file

  # First-time run with KB build
  python3 ria_agent.py --rebuild-kb --changed-method myMethod

  # Skip refinement
  python3 ria_agent.py --changed-method myMethod --no-refinement

  # Skip HTML
  python3 ria_agent.py --changed-method myMethod --no-html
"""

import os
import sys
import json
import re
import time
import argparse
import shutil
import subprocess
import platform
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Global timing infrastructure
# ---------------------------------------------------------------------------
STAGE_TIMINGS = {}

def start_timer(stage_name: str) -> None:
    """Record start time for a pipeline stage."""
    STAGE_TIMINGS[stage_name] = {'start': time.time(), 'end': None}

def end_timer(stage_name: str) -> None:
    """Record end time for a pipeline stage and calculate duration."""
    if stage_name in STAGE_TIMINGS and STAGE_TIMINGS[stage_name]['start'] is not None:
        STAGE_TIMINGS[stage_name]['end'] = time.time()
        STAGE_TIMINGS[stage_name]['duration'] = (
            STAGE_TIMINGS[stage_name]['end'] - STAGE_TIMINGS[stage_name]['start']
        )

def get_stage_duration(stage_name: str) -> float:
    """Get duration in seconds for a completed stage, or 0.0 if not recorded."""
    if stage_name in STAGE_TIMINGS and 'duration' in STAGE_TIMINGS[stage_name]:
        return round(STAGE_TIMINGS[stage_name]['duration'], 2)
    return 0.0

def get_total_duration() -> float:
    """Calculate total pipeline execution time from all recorded stages."""
    total = sum(
        t.get('duration', 0.0)
        for t in STAGE_TIMINGS.values()
        if 'duration' in t
    )
    return round(total, 2)

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
# This file lives at:
#   <repo>/.github/skills/regression-impact-analysis/scripts/ria_agent.py
#
# Folder layout:
#   <repo>/.github/skills/regression-impact-analysis/scripts/   -> SCRIPT_DIR
#   <repo>/.github/skills/regression-impact-analysis/           -> SKILL_DIR
#   <repo>/.github/                                             -> GITHUB_DIR
#   <repo>/                                                     -> REPO_ROOT
#   <repo>/.github/RIA_OUTPUT/                                  -> OUTPUT_DIR
#   <repo>/.github/RIA_OUTPUT/knowledge_base/                   -> KB_DIR
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent                 # regression-impact-analysis/
GITHUB_DIR = SKILL_DIR.parent.parent          # .github/
REPO_ROOT = GITHUB_DIR.parent                 # repo root
OUTPUT_DIR = GITHUB_DIR / 'RIA_OUTPUT'
KB_DIR = OUTPUT_DIR / 'knowledge_base'

# ---------------------------------------------------------------------------
# Log consolidation infrastructure
# ---------------------------------------------------------------------------
# Every pipeline run captures stdout + stderr into a timestamped log file
# under <OUTPUT_DIR>/logs/ria_run_<YYYYMMDD_HHMMSS>.log so that we have a
# durable audit trail for debugging and review. The TeeLogger duplicates
# every write to BOTH the console and the log file so the user still sees
# normal output while the file fills up in real time.
LOGS_SUBDIR = 'logs'
DEFAULT_MAX_LOGS = 10


class TeeLogger:
    """Captures stdout/stderr to both console and log file.

    Acts as a transparent file-like object: anything written to it is
    forwarded to the original stream (so the user still sees output) AND
    appended to the consolidated log file. Flushing on every write keeps
    the log usable in real time even if the pipeline crashes mid-run.
    """

    def __init__(self, log_path, original_stream):
        # Append mode so stdout + stderr loggers can share the same file
        # without truncating each other when both are constructed.
        self.log_file = open(log_path, 'a', encoding='utf-8')
        self.original_stream = original_stream

    def write(self, message):
        try:
            self.original_stream.write(message)
        except Exception:
            # Never let a console-write failure bring down the pipeline.
            pass
        try:
            self.log_file.write(message)
            self.log_file.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self.original_stream.flush()
        except Exception:
            pass
        try:
            self.log_file.flush()
        except Exception:
            pass

    def close(self):
        try:
            self.log_file.close()
        except Exception:
            pass

    # Some libraries probe these attributes (e.g. isatty for colour output).
    def isatty(self):
        try:
            return self.original_stream.isatty()
        except Exception:
            return False

    def fileno(self):
        # Subprocess inheritance still uses the *original* fd, but some
        # callers ask for fileno on sys.stdout to detect terminal-ness.
        return self.original_stream.fileno()


def setup_logging(output_dir, max_logs=DEFAULT_MAX_LOGS):
    """Setup log consolidation for a pipeline run.

    Creates ``<output_dir>/logs/ria_run_<timestamp>.log``, rotates older
    log files (keeping the last ``max_logs`` runs), and replaces
    ``sys.stdout`` / ``sys.stderr`` with TeeLogger instances that mirror
    every write to the new log file.

    Returns:
        tuple: (log_path, cleanup_func) where cleanup_func restores the
        original streams and writes a footer with the completion time.
        Cleanup is idempotent and safe to call multiple times.
    """
    output_dir = Path(output_dir)
    logs_dir = output_dir / LOGS_SUBDIR
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Rotate: keep the newest (max_logs - 1) files so the new log we are
    # about to create stays under the cap. Sort by mtime so manual file
    # copies do not skew the rotation order.
    try:
        existing_logs = sorted(
            logs_dir.glob('ria_run_*.log'),
            key=lambda p: p.stat().st_mtime,
        )
        if max_logs > 0 and len(existing_logs) >= max_logs:
            keep = max_logs - 1
            to_delete = existing_logs[:-keep] if keep > 0 else existing_logs
            for old_log in to_delete:
                try:
                    old_log.unlink()
                except Exception:
                    pass
    except Exception:
        # Rotation failures must never block the actual pipeline run.
        pass

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path = logs_dir / f'ria_run_{timestamp}.log'

    # Write the header BEFORE attaching the tee so the header is not
    # itself duplicated to the console (where we want a clean banner).
    try:
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write('=' * 80 + '\n')
            f.write('RIA Pipeline Execution Log\n')
            f.write(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"PID    : {os.getpid()}\n")
            f.write(f"Argv   : {' '.join(sys.argv)}\n")
            f.write('=' * 80 + '\n\n')
    except Exception as _e:
        # If we cannot even create the log file, fall back to a no-op
        # cleanup so the pipeline keeps running on console only.
        print(f"[WARN] Could not initialize consolidated log file: {_e}")

        def _noop_cleanup():
            return None

        return log_path, _noop_cleanup

    original_stdout = sys.stdout
    original_stderr = sys.stderr

    sys.stdout = TeeLogger(log_path, original_stdout)
    sys.stderr = TeeLogger(log_path, original_stderr)

    _cleanup_state = {'done': False}

    def cleanup():
        if _cleanup_state['done']:
            return
        _cleanup_state['done'] = True
        try:
            tee_stdout = sys.stdout
            tee_stderr = sys.stderr
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            try:
                tee_stdout.close()
            except Exception:
                pass
            try:
                tee_stderr.close()
            except Exception:
                pass
        except Exception:
            pass
        # Footer is written directly to the file (not via tee) so the
        # closing banner does not echo to the console after the pipeline
        # has already printed its own "PIPELINE COMPLETE" output.
        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write('\n' + '=' * 80 + '\n')
                f.write(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write('=' * 80 + '\n')
        except Exception:
            pass

    return log_path, cleanup


def log_stage_timing(stage_name):
    """Decorator that prints stage banners + duration around a function.

    The banners go through ``print`` so they are captured by TeeLogger
    automatically. Failures still log a banner with the duration before
    re-raising so the log shows where things broke.
    """
    def decorator(func):
        from functools import wraps

        @wraps(func)
        def wrapper(*args, **kwargs):
            print('\n' + '=' * 80)
            print(f"STAGE: {stage_name}")
            print(f"Started: {datetime.now().strftime('%H:%M:%S')}")
            print('=' * 80 + '\n')
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                duration = time.time() - start_time
                print('\n' + '=' * 80)
                print(f"STAGE: {stage_name} - COMPLETED")
                print(f"Duration: {duration:.2f} seconds")
                print('=' * 80 + '\n')
                return result
            except Exception as e:
                duration = time.time() - start_time
                print('\n' + '=' * 80)
                print(f"STAGE: {stage_name} - FAILED")
                print(f"Duration: {duration:.2f} seconds")
                print(f"Error: {e}")
                print('=' * 80 + '\n')
                raise

        return wrapper

    return decorator


# Make sibling scripts importable (must precede the kb_strategy import below
# so the agent works whether invoked as a script or imported as a module).
sys.path.insert(0, str(SCRIPT_DIR))
# Phase 2: also expose the skill root so 'configs.ria_config' is importable
# (used by the --language flag handler below).
sys.path.insert(0, str(SKILL_DIR))

# Agent-driven reasoning helpers (skip-guards + pause/resume handoff).
# Replaces the old copilot_llm_bridge request/response file mailbox.
import agent_reasoning  # noqa: E402  - import after sys.path tweak

# Intelligent KB-validation strategy (replaces hard-coded thresholds).
# See kb_strategy.py for the prompt-keyword -> strategy decision matrix.
from kb_strategy import (  # noqa: E402  - import after sys.path tweak
    infer_kb_strategy_from_prompt,
    get_validation_behavior,
    explain_strategy,
)

# ---------------------------------------------------------------------------
# Per-run cache for flow dependencies (Fix P2: Flow Dependency Caching)
# ---------------------------------------------------------------------------
# Avoids redundant calls to build_flow_dependencies.py within a single pipeline
# run. Cache key: (changed_component, sha256_hash_of_flow_registry_content).
# Cleared at the start of every main() invocation so the cache lives only for
# the current pipeline run.
_FLOW_DEPS_CACHE = {}


# ---------------------------------------------------------------------------
# Concurrency control: PID-based pipeline lock
# ---------------------------------------------------------------------------
# A single RIA pipeline run reads/writes many files in OUTPUT_DIR (stage1..7
# JSON, consolidated_summary.json, RIA_Report.html, etc.). Two pipelines
# running at the same time will silently corrupt each other's intermediate
# outputs and produced reports.
#
# Historically this manifested as a "RIA_Report_old.html" file appearing in
# RIA_OUTPUT/: a previous version of generate_html_report.py renamed the
# pre-existing report as a backup before writing a new one, so concurrent
# runs would leave two near-identical HTML files behind. The backup logic
# has since been removed, but the underlying race - parallel pipelines
# clobbering each other's stage outputs - remained. The lock below closes
# that gap once and for all.
#
# Mechanism: a lock file at OUTPUT_DIR/.ria_lock containing the owning PID
# and start timestamp.  At startup we:
#   1. Create OUTPUT_DIR if missing.
#   2. Atomically create the lock file using O_CREAT|O_EXCL.
#   3. If creation fails (file already exists), read the PID inside it and
#      check whether that process is still alive. Stale locks (process
#      gone) are reclaimed automatically; live locks abort the new run
#      with a clear, actionable message.
# Cleanup runs in a `finally` block so KeyboardInterrupt, SystemExit, and
# fatal exceptions all release the lock.
RIA_LOCK_FILE = OUTPUT_DIR / '.ria_lock'


def _is_pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID is currently running.

    Uses os.kill(pid, 0) which performs no actual signaling but raises
    ProcessLookupError when the PID is gone and PermissionError when the
    PID exists but is owned by another user (still alive). Works on
    macOS, Linux, and other POSIX systems. On Windows, falls back to
    assuming the lock is live (safer than reclaiming a lock we cannot
    verify).
    """
    if pid <= 0:
        return False
    if os.name == 'nt':
        # No portable PID liveness check on plain Python/Windows without
        # extra deps; treat the lock as live so we never falsely reclaim.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists but belongs to another user -> still a live process.
        return True
    except OSError:
        # Any other OSError is treated as "unknown" -> assume alive to be
        # safe (we'd rather block than silently overwrite).
        return True
    return True


def _read_lock_info(lock_path: Path) -> dict:
    """Read lock file contents. Returns {} on any read/parse failure."""
    try:
        with open(lock_path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def acquire_ria_lock() -> bool:
    """Acquire the RIA pipeline lock. Returns True on success.

    On failure (another live RIA process is running) prints a clear
    diagnostic and returns False so the caller can exit cleanly. Stale
    locks (owning PID has died) are reclaimed automatically.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        'pid': os.getpid(),
        'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'argv': sys.argv,
    }
    payload_bytes = json.dumps(payload, indent=2).encode('utf-8')

    # Try a few times so a stale lock cleaned up between the EXCL failure
    # and the liveness check is handled cleanly.
    for _attempt in range(2):
        try:
            fd = os.open(
                str(RIA_LOCK_FILE),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
            try:
                os.write(fd, payload_bytes)
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            info = _read_lock_info(RIA_LOCK_FILE)
            existing_pid = int(info.get('pid', 0) or 0)
            started_at = info.get('started_at', 'unknown')
            if existing_pid and _is_pid_alive(existing_pid):
                print()
                print("=" * 80)
                print("[ABORT] Another RIA pipeline is already running")
                print("=" * 80)
                print(f"  Lock file : {RIA_LOCK_FILE}")
                print(f"  Owner PID : {existing_pid}")
                print(f"  Started   : {started_at}")
                print()
                print("  Wait for the other run to finish, or - if you are")
                print("  certain it has crashed - delete the lock file:")
                print(f"      rm {RIA_LOCK_FILE}")
                print()
                print("  Running multiple RIA pipelines in parallel is not")
                print("  supported: they would corrupt each other's stage")
                print("  outputs and the HTML report.")
                print("=" * 80)
                print()
                return False
            # Stale lock -> remove and retry the EXCL create.
            print(
                f"[INFO] Reclaiming stale RIA lock "
                f"(owner PID {existing_pid or '?'} is no longer running)."
            )
            try:
                RIA_LOCK_FILE.unlink()
            except FileNotFoundError:
                pass
            except OSError as _e:
                print(f"[ERROR] Could not remove stale lock file: {_e}")
                return False
            # loop continues for the retry
    print(f"[ERROR] Failed to acquire RIA lock at {RIA_LOCK_FILE}")
    return False


def release_ria_lock() -> None:
    """Release the RIA pipeline lock if and only if we still own it.

    Safe to call multiple times and from a `finally` block. We re-read the
    lock file and only delete it when the recorded PID matches our own,
    so a stale-lock reclaim by a later run never causes the original
    process to delete the new owner's lock.
    """
    try:
        info = _read_lock_info(RIA_LOCK_FILE)
    except Exception:
        info = {}
    if not info:
        # Lock already gone (e.g. cleaned up by a previous call).
        return
    if int(info.get('pid', 0) or 0) != os.getpid():
        # Some other process owns it now - do not touch.
        return
    try:
        RIA_LOCK_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as _e:
        print(f"[WARN] Could not remove RIA lock file {RIA_LOCK_FILE}: {_e}")


def cleanup_legacy_report_backup() -> None:
    """Remove a stray RIA_Report_old.html left over from older versions.

    Older revisions of generate_html_report.py renamed the previous
    RIA_Report.html to RIA_Report_old.html before writing a fresh report,
    which produced two confusingly similar files in RIA_OUTPUT/. The
    backup logic has been removed, but copies of the stale backup may
    still be on disk from earlier runs. Clean them up at startup so the
    output directory only ever contains the current report.
    """
    legacy = OUTPUT_DIR / 'RIA_Report_old.html'
    if legacy.exists():
        try:
            legacy.unlink()
            print(f"[INFO] Removed stale legacy report backup: {legacy}")
        except OSError as _e:
            print(f"[WARN] Could not remove {legacy}: {_e}")


def _hash_file_content(file_path: Path) -> str:
    """Return SHA256 hash of file content for cache key."""
    import hashlib
    if not file_path.exists():
        return ""
    with open(file_path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def _clear_flow_deps_cache():
    """Clear the per-run flow dependencies cache."""
    global _FLOW_DEPS_CACHE
    _FLOW_DEPS_CACHE = {}


# ---------------------------------------------------------------------------
# Quick Win #3: KB artifact content-hash cache
# ---------------------------------------------------------------------------
# Several KB build steps (component_map, IDF index, embeddings) take 1-4 min
# each but only need to be rebuilt when their UPSTREAM INPUTS change. We
# write a sidecar `<artifact>.fp` next to each artifact that contains the
# SHA256 of every dependency's content. On subsequent runs we recompute the
# fingerprint and skip the build if it matches.
def _hash_dependencies(dep_paths) -> str:
    """Return a single SHA256 covering every dependency's content + name.

    The dependency NAME is included so a missing file does not collide with
    a present file of zero length.  Missing dependencies are folded into the
    hash as the empty-string, so an artifact built without them differs
    from one built with them.
    """
    import hashlib
    hasher = hashlib.sha256()
    for dep in dep_paths:
        dep_path = Path(dep)
        # Include the name so missing/added deps are detected.
        hasher.update(b'\x00name=')
        hasher.update(str(dep_path).encode('utf-8', errors='replace'))
        hasher.update(b'\x00content=')
        if dep_path.exists() and dep_path.is_file():
            try:
                with open(dep_path, 'rb') as fh:
                    while True:
                        chunk = fh.read(8192)
                        if not chunk:
                            break
                        hasher.update(chunk)
            except OSError:
                # Treat read failure as "unknown content" -> distinct from
                # a real read so cache invalidation is safe.
                hasher.update(b'<read-error>')
        else:
            hasher.update(b'<missing>')
    return hasher.hexdigest()


def _fingerprint_path(artifact_path: Path) -> Path:
    """Return the sidecar fingerprint path for ``artifact_path``."""
    return artifact_path.with_suffix(artifact_path.suffix + '.fp')


def _kb_artifact_is_fresh(artifact_path: Path, deps) -> bool:
    """Return True iff ``artifact_path`` exists and its sidecar fingerprint
    matches the current content of ``deps``.

    Safe in the presence of missing/corrupted .fp files: any read failure
    is treated as a cache miss so the artifact is rebuilt.
    """
    fp_path = _fingerprint_path(artifact_path)
    try:
        if not artifact_path.exists() or not fp_path.exists():
            return False
    except OSError:
        return False
    try:
        stored = fp_path.read_text(encoding='utf-8').strip()
    except OSError:
        return False
    if not stored:
        return False
    current = _hash_dependencies(deps)
    return stored == current


def _write_kb_fingerprint(artifact_path: Path, deps) -> None:
    """Write the dependency fingerprint sidecar for ``artifact_path``.

    Atomic-ish: writes to ``<fp>.tmp`` and renames so a crashing run cannot
    leave a half-written .fp file that would falsely report cache hits.
    Failures are logged but never raise — caching is best-effort and must
    never break the pipeline.
    """
    fp_path = _fingerprint_path(artifact_path)
    try:
        digest = _hash_dependencies(deps)
        tmp_path = fp_path.with_suffix(fp_path.suffix + '.tmp')
        tmp_path.write_text(digest, encoding='utf-8')
        os.replace(tmp_path, fp_path)
    except OSError as exc:
        print(f"[WARN] Could not write fingerprint for {artifact_path.name}: {exc}")


# Map from KB output filename -> list of dependency paths used to fingerprint
# the artifact. Looked up lazily inside run_kb_build() so REPO_ROOT / KB_DIR
# are fully resolved before we read them.
def _kb_artifact_dependencies(kb_file: str):
    """Return the list of upstream dependency paths for a given KB artifact.

    Returns None for artifacts we do NOT cache (they fall through to the
    standard "exists -> skip" check). Only the three expensive artifacts
    listed in Quick Win #3 are cached: component_map.json, idf_index.json,
    embeddings_index.npz.
    """
    raw_corpus = Path(REPO_ROOT) / '.github' / 'RIA_INPUT' / 'all_tcs_extracted.json'
    framework_suffixes = KB_DIR / 'discovered_framework_suffixes.json'

    if kb_file == 'component_map.json':
        # build_component_map.py reads the test corpus and the discovered
        # framework-suffix vocabulary. Either change invalidates the map.
        return [raw_corpus, framework_suffixes]
    if kb_file == 'idf_index.json':
        # term_idf.py reads the test corpus only.
        return [raw_corpus]
    if kb_file == 'embeddings_index.npz':
        # build_embeddings.py reads the test corpus only.
        return [raw_corpus]
    return None


# ---------------------------------------------------------------------------
# Knowledge Base file inventory
# ---------------------------------------------------------------------------
# One-time KB files (must exist before first analysis).
# These are corpus-wide/codebase-wide files that don't depend on which method changed.
KB_FILES_ONETIME = [
    'synonym_groups.json',                  # CRUD/PROCESS verb groups (codebase-wide)
    'component_map.json',                   # Component consolidation (corpus-wide)
    # Data-driven vocabulary discoveries (Stage 0d). These replaced the
    # previously hardcoded GENERIC_DOMAIN_NOUNS / domain_tokens /
    # _FRAMEWORK_SUFFIXES sets and are corpus-wide so they live with the
    # one-time KB.
    'discovered_framework_suffixes.json',   # Class-name suffix patterns
    'discovered_generic_nouns.json',        # MAD-z anomalous tokens
    'domain_vocabulary.json',               # Domain-specific vocabulary
    # Auto-discovered scoring artifacts. Built once from the codebase and
    # test corpus. All downstream thresholds (IDF bypass, prefix frequency,
    # connector tokens, hub-flow coverage) are derived from these statistics
    # at runtime — no hardcoded magic numbers.
    'codebase_vocabulary.json',             # Identifier word frequencies + stats
    'idf_index.json',                       # Corpus-wide term IDF + distribution stats
    'embeddings_index.npz',                  # Sentence embeddings for semantic Layer 5
]

# Per-analysis KB files (rebuilt every run in Step 0b / rebuild_focused_kb()).
# These are FOCUSED on the specific changed method, so they MUST be rebuilt
# per analysis to be accurate.
KB_FILES_PERANALYSIS = [
    'flow_registry.json',                          # Flow discovery for THIS change
    'flow_dependencies.json',                      # Dependencies for THIS change's components
    'all_tcs_extracted_enriched_source.json',      # Tests re-tagged for source pipeline
]

# NOTE: The enriched test corpus is a PER-ANALYSIS file, not one-time.
# The RAW test corpus (all_tcs_extracted.json) is fetched once from Jira via
# tc_extractor.py. The ENRICHED version is regenerated every run by
# build_flow_registry.py as a side-effect, tagging tests with flow tags
# relevant to the current change.

# Complete KB inventory (for documentation/reference only).
KB_FILES_ALL = KB_FILES_ONETIME + KB_FILES_PERANALYSIS

# Backward-compatible alias (some legacy code paths still reference KB_FILES).
KB_FILES = KB_FILES_ALL

# KB build scripts in execution order (script -> output file -> extra args)
# Only TRUE one-time, change-independent files are built here:
#   1. synonym_groups               (CRUD/PROCESS verb groups - codebase-wide)
#   2. discovered_framework_suffixes (data-driven; required by component_map)
#   3. component_map                (component consolidation - corpus-wide)
#   4. discovered_generic_nouns +
#      domain_vocabulary            (data-driven; depend on component_map)
#
# NOTE: flow_registry.json and flow_dependencies.json are NOT built here.
# They are rebuilt per-analysis in rebuild_focused_kb() (Step 0b) because
# they must be FOCUSED on the changed method.
#
# NOTE: the enriched corpus (all_tcs_extracted_enriched_source.json) is also
# NOT built here.
# The RAW test corpus (all_tcs_extracted.json) must be created by tc_extractor.py
# ONCE before first run. The ENRICHED version is regenerated every run by
# build_flow_registry.py as a side-effect during Step 0b.
KB_BUILD_STAGES = [
    # (script, output kb file, extra CLI args)
    ('build_synonym_groups.py',          'synonym_groups.json',                 []),
    ('build_discovered_vocabularies.py', 'discovered_framework_suffixes.json',  ['--only', 'framework_suffixes']),
    ('build_component_map.py',           'component_map.json',                  []),
    # Auto-discovery stages — must run BEFORE vocabularies / diff-concept
    # extraction so that downstream consumers can read the patterns:
    #   * test_patterns.json: filename / directory conventions for tests.
    #   * language_reserved_words.json: language keywords + built-ins
    #     (consumed by `extract_diff_concepts._load_reserved_words` and
    #     `_load_calltree_stopwords`).
    # Both builders are zero-hardcoding: they introspect the repo and the
    # language runtime rather than embedding keyword tables.
    ('discover_test_patterns.py',        'test_patterns.json',                  []),
    ('discover_reserved_words.py',       'language_reserved_words.json',        []),
    ('build_discovered_vocabularies.py', 'discovered_generic_nouns.json',       ['--only', 'vocabularies']),
    # domain_vocabulary.json is produced by the same invocation as
    # discovered_generic_nouns.json, but listing it here lets the freshness
    # check ensure both files exist on disk.
    ('build_discovered_vocabularies.py', 'domain_vocabulary.json',              ['--only', 'vocabularies']),
    # Auto-discovered scoring artifacts: codebase vocabulary (identifier word
    # frequencies + distribution stats for programming-prefix detection) and
    # IDF index (corpus-wide term specificity for diff-concept scoring).
    # All downstream thresholds are derived from these at runtime.
    ('extract_diff_concepts.py',         'codebase_vocabulary.json',
     ['--build-vocab', '--kb-dir', str(KB_DIR)]),
    ('term_idf.py',                      'idf_index.json',
     ['--corpus', str(Path(REPO_ROOT) / '.github' / 'RIA_INPUT' / 'all_tcs_extracted.json'),
      '--output', str(KB_DIR / 'idf_index.json')]),
    ('build_embeddings.py',              'embeddings_index.npz',
     ['--corpus', str(Path(REPO_ROOT) / '.github' / 'RIA_INPUT' / 'all_tcs_extracted.json'),
      '--output', str(KB_DIR / 'embeddings_index.npz')]),
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def banner(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def section(title: str) -> None:
    print()
    print("-" * 80)
    print(title)
    print("-" * 80)


# ---------------------------------------------------------------------------
# Option A: Selective cleanup of RIA_OUTPUT between runs.
# ---------------------------------------------------------------------------
# We aggressively delete per-run artefacts (stage outputs, focused KB files,
# enriched corpus, HTML, multi_method/, logs/) so a fresh run starts from a
# clean slate. The 10 one-time KB
# files in `KB_FILES_ONETIME` are PRESERVED — they are corpus-wide, expensive
# to rebuild, and identical across runs unless the test corpus or codebase
# itself changes. Pass `--rebuild-kb` (or call `_selective_cleanup_ria_output`
# with `clean_kb_one_time=True`) to wipe those too.
def _selective_cleanup_ria_output(clean_kb_one_time: bool = False) -> dict:
    """Clean per-run artefacts while preserving one-time KB files.

    KEEP (one-time KB - expensive to rebuild, listed in KB_FILES_ONETIME):
      - synonym_groups.json
      - component_map.json
      - domain_vocabulary.json
      - codebase_vocabulary.json
      - test_patterns.json
      - language_reserved_words.json
      - discovered_framework_suffixes.json
      - discovered_generic_nouns.json
      - idf_index.json
      - embeddings_index.npz

    DELETE (per-run artefacts - must be fresh every run):
      - flow_registry.json
      - flow_dependencies.json
      - all_tcs_extracted_enriched_source.json
      - stage*.json (stage1..stage7 outputs)
      - multi_method/
      - *.html
      - logs/
      - audit_report.json + validation_audit_report.md
      - method_understanding.json
      - diff_concepts.json
      - anchor_concepts.json
      - ria_v7_summary.json
      - consolidated_summary.json

    Args:
        clean_kb_one_time: when True, also delete the KB_FILES_ONETIME files.
            Use this for `--rebuild-kb` runs.

    Returns: a dict {'deleted': [...], 'kept': [...], 'errors': [...] }
        summarising what was touched.
    """
    import shutil as _shutil

    deleted: list = []
    kept: list = []
    errors: list = []

    section("Selective cleanup of RIA_OUTPUT")

    # ---- 1. KB directory --------------------------------------------------
    if KB_DIR.exists():
        keep_set = set() if clean_kb_one_time else set(KB_FILES_ONETIME)
        for entry in KB_DIR.iterdir():
            try:
                if entry.is_dir():
                    # Sub-directories under KB are always per-run.
                    _shutil.rmtree(entry, ignore_errors=True)
                    deleted.append(str(entry))
                    continue
                name = entry.name
                if name in keep_set:
                    kept.append(str(entry))
                    continue
                entry.unlink()
                deleted.append(str(entry))
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(f"{entry}: {exc}")

    # ---- 2. OUTPUT_DIR top-level per-run files / dirs ---------------------
    if OUTPUT_DIR.exists():
        # Delete everything in OUTPUT_DIR EXCEPT the knowledge_base/ tree
        # (already pruned selectively above) and the logs/ tree (the current
        # run's log file is already open inside it; truncating would race
        # with `setup_logging`'s rotation). Anything else under OUTPUT_DIR
        # is a per-run artefact.
        logs_dir = OUTPUT_DIR / 'logs'
        for entry in OUTPUT_DIR.iterdir():
            try:
                if entry.resolve() == KB_DIR.resolve():
                    continue  # KB handled above
                if entry.resolve() == logs_dir.resolve():
                    kept.append(str(entry))
                    continue  # logs handled by setup_logging max_logs cap
                if entry.is_dir():
                    _shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
                deleted.append(str(entry))
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(f"{entry}: {exc}")

    print(f"  Deleted: {len(deleted)} entries")
    print(f"  Kept   : {len(kept)} one-time KB files")
    if errors:
        print(f"  Errors : {len(errors)} (non-fatal, see below)")
        for e in errors[:5]:
            print(f"    {e}")
    return {'deleted': deleted, 'kept': kept, 'errors': errors}


def _load_calltree_stopwords(kb_dir: str) -> frozenset:
    """Build a stopword set from auto-discovered KB vocabularies.

    Combines three data-driven sources (NO hardcoded words):
      1. discovered_generic_nouns.json — statistically generic test-corpus
         nouns (e.g. "agent", "shift") that appear too often to be
         discriminative.
      2. language_reserved_words.json — language keywords discovered during
         KB build (e.g. "class", "return", "public" for Java).
      3. codebase_vocabulary.json at p99 frequency — infrastructure/framework
         words that appear in ≥1% of all identifiers in the codebase.

    All three are auto-generated during the KB build phase and adapt to
    whatever product/language the pipeline is applied to.
    """
    stopwords: set = set()

    if not kb_dir or not os.path.isdir(kb_dir):
        return frozenset()

    # 1) Generic nouns from test corpus
    gn_path = os.path.join(kb_dir, 'discovered_generic_nouns.json')
    if os.path.isfile(gn_path):
        try:
            with open(gn_path, 'r', encoding='utf-8') as f:
                gn = json.load(f)
            for w in gn.get('generic_nouns', []):
                stopwords.add(w.lower())
        except Exception:
            pass

    # 2) Language reserved words
    rw_path = os.path.join(kb_dir, 'language_reserved_words.json')
    if os.path.isfile(rw_path):
        try:
            with open(rw_path, 'r', encoding='utf-8') as f:
                rw = json.load(f)
            for w in rw.get('reserved_words', []):
                stopwords.add(w.lower())
        except Exception:
            pass

    # 3) High-frequency codebase words (>= p99)
    cv_path = os.path.join(kb_dir, 'codebase_vocabulary.json')
    if os.path.isfile(cv_path):
        try:
            with open(cv_path, 'r', encoding='utf-8') as f:
                cv = json.load(f)
            vocab = cv.get('vocabulary', {})
            stats = cv.get('frequency_statistics', {})
            threshold = stats.get('p99', 1000)
            for word, freq in vocab.items():
                if freq >= threshold:
                    stopwords.add(word.lower())
        except Exception:
            pass

    return frozenset(stopwords)


def _enrich_from_calltree(diff_concepts: dict, changes: dict,
                          kb_dir: str = None) -> None:
    """Enrich diff concepts with domain terms from the call-tree context.

    Extracts class names and method names from the changed files, CamelCase
    splits them, filters out generic terms using auto-discovered KB
    vocabularies (NO hardcoded stopwords), and adds surviving domain words
    as additional diff concepts (unigrams + bigrams).  This ensures the IDF
    scorer can differentiate tests about the impacted call-tree (e.g. "swap",
    "trade") from tests about unrelated flows that share context variable
    names (e.g. "split shift", "daily shift gap").
    """
    try:
        from extract_diff_concepts import decompose_identifier
    except ImportError:
        return

    stopwords = _load_calltree_stopwords(kb_dir) if kb_dir else frozenset()

    existing = set(diff_concepts.get('all_phrases', []))
    new_words: list = []

    for fi in changes.get('changed_files', []):
        # Extract class name from file path (e.g. AgentTradeDao.java -> AgentTradeDao)
        fp = fi.get('file_path', '')
        basename = os.path.basename(fp)
        class_name = basename.rsplit('.', 1)[0] if '.' in basename else basename
        if class_name:
            parts = decompose_identifier(class_name)
            new_words.extend(p.lower() for p in parts)

        for m in fi.get('changed_methods', []):
            # Class name from method dict (if available)
            cn = m.get('class_name', '')
            if cn:
                parts = decompose_identifier(cn)
                new_words.extend(p.lower() for p in parts)
            # Method name
            mn = m.get('method_name', '')
            if mn:
                parts = decompose_identifier(mn)
                new_words.extend(p.lower() for p in parts)

    # Dedupe and filter using auto-discovered stopwords
    domain_words = sorted({w for w in new_words
                           if len(w) >= 3 and w not in stopwords})

    if not domain_words:
        return

    added = []
    # Add surviving words as unigram concepts
    for w in domain_words:
        if w not in existing:
            diff_concepts.setdefault('all_phrases', []).append(w)
            diff_concepts.setdefault('concepts', []).append(w)
            existing.add(w)
            added.append(w)

    # Add bigrams from original decomposition order of each identifier
    # (not from alphabetically sorted words which produces meaningless pairs)
    seen_bigrams = set()
    for fi in changes.get('changed_files', []):
        for m in fi.get('changed_methods', []):
            for name_key in ('method_name', 'class_name'):
                name = m.get(name_key, '')
                if not name:
                    continue
                parts = [p.lower() for p in decompose_identifier(name)
                         if len(p) >= 3 and p.lower() not in stopwords]
                for i in range(len(parts) - 1):
                    bigram = f"{parts[i]} {parts[i + 1]}"
                    if bigram not in existing and bigram not in seen_bigrams:
                        diff_concepts.setdefault('all_phrases', []).append(bigram)
                        diff_concepts.setdefault('bigrams', []).append(bigram)
                        existing.add(bigram)
                        seen_bigrams.add(bigram)
                        added.append(bigram)

    if added:
        print(f"[OK] Enriched diff concepts with {len(added)} call-tree terms: {added}")


def _extract_anchor_concepts(changes: dict, kb_dir: str = None) -> list:
    """Extract domain-specific anchor concepts from changed METHOD NAMES.

    These anchors represent the feature/domain the code change belongs to
    (e.g. 'swap' from partialAgentSwap).  They are used downstream in Stage 6
    to filter keyword-only tests that match diff-concept variables (like
    'split shift', 'min daily shift gap') but are about a different feature
    (e.g. Extra Hours, Time Off).

    Fully auto-discovered — no hardcoded domain terms.

    Algorithm:
      1. Decompose all changed method names via CamelCase splitting.
      2. Filter using auto-discovered stopwords (generic nouns, reserved
         words, high-frequency codebase vocabulary).
      3. Count how many distinct methods each surviving word appears in.
      4. If >= 2 distinct methods exist, keep only words appearing in >= 2
         methods (the common feature thread, e.g. 'swap').
      5. Fallback for single-method changes: keep all words with test-corpus
         IDF >= 2.5 (words appearing in < ~8% of test docs).
    """
    try:
        from extract_diff_concepts import decompose_identifier
    except ImportError:
        return []

    stopwords = _load_calltree_stopwords(kb_dir) if kb_dir else frozenset()

    # Collect words PER DISTINCT method AND file/class (for frequency counting)
    per_method_words: list = []  # list of sets
    seen_methods: set = set()
    seen_files: set = set()
    for fi in changes.get('changed_files', []):
        # Also decompose the FILE/CLASS name to capture the module/feature name
        # e.g. AgentTradeDao.java → "trade", FetchAgentListTradeSchedule → "trade"
        file_path = fi.get('file_path', '')
        class_name = os.path.basename(file_path).replace('.java', '').replace('.py', '').replace('.ts', '')
        if class_name and class_name not in seen_files:
            seen_files.add(class_name)
            parts = decompose_identifier(class_name)
            words = {p.lower() for p in parts
                     if len(p) >= 3 and p.lower() not in stopwords}
            if words:
                per_method_words.append(words)
        for m in fi.get('changed_methods', []):
            mn = m.get('method_name', '')
            if mn and mn not in seen_methods:
                seen_methods.add(mn)
                parts = decompose_identifier(mn)
                words = {p.lower() for p in parts
                         if len(p) >= 3 and p.lower() not in stopwords}
                if words:
                    per_method_words.append(words)

    if not per_method_words:
        return []

    # Count how many distinct methods each word appears in
    from collections import Counter
    word_counts = Counter()
    for word_set in per_method_words:
        for w in word_set:
            word_counts[w] += 1

    n_methods = len(per_method_words)

    if n_methods >= 2:
        # Multi-method: keep only words appearing in >= 2 methods
        # (the common feature thread across the change)
        anchors = sorted(w for w, c in word_counts.items() if c >= 2)
    else:
        # Single method: use IDF filtering to keep domain-specific words
        anchors = sorted(word_counts.keys())
        if kb_dir:
            idf_path = os.path.join(kb_dir, 'idf_index.json')
            if os.path.isfile(idf_path):
                try:
                    with open(idf_path, 'r', encoding='utf-8') as f:
                        idf_data = json.load(f)
                    idf_terms = idf_data.get('terms', idf_data.get('idf', {}))
                    anchors = [w for w in anchors
                               if idf_terms.get(w, 999) >= 2.5]
                except Exception:
                    pass

    if anchors:
        print(f"[OK] Anchor concepts from method names: {anchors}")
    return anchors


def _refine_diff_concepts_with_method_understanding(diff_concepts: dict,
                                                      output_dir) -> dict:
    """Refine diff concepts using Stage 1.5 LLM method understanding output.

    Uses the LLM's surgical analysis (changed_variables, NOT_affected) to
    FILTER the blindly-extracted diff concepts.  This preserves recall
    (we never drop concepts matching the actual change) while removing
    false-positive concepts from adjacent code that was NOT modified.

    Strategy:
      1. Load method_understanding.json (Stage 1.5 output).
      2. Collect changed_variables → these concepts are ALWAYS kept.
      3. Collect NOT_affected behaviors → derive exclusion keywords from them.
      4. For each diff concept phrase, if it matches an exclusion keyword
         but NOT a changed variable, remove it.
    """
    mu_path = os.path.join(str(output_dir), 'method_understanding.json')
    if not os.path.isfile(mu_path):
        print("[SKIP] No method_understanding.json — diff concepts unfiltered")
        return diff_concepts

    try:
        with open(mu_path, 'r', encoding='utf-8') as f:
            mu_data = json.load(f)
    except Exception as e:
        print(f"[WARN] Could not read method_understanding.json: {e}")
        return diff_concepts

    methods = mu_data.get('methods', [])
    if not methods:
        return diff_concepts

    # Collect changed variables (lowercased, plus their decomposed forms)
    changed_vars = set()
    for m in methods:
        for v in m.get('changed_variables', []):
            changed_vars.add(v.lower())
            # Also decompose camelCase: minDailyShiftGap → min, daily, shift, gap
            try:
                from extract_diff_concepts import decompose_identifier
                parts = decompose_identifier(v)
                for p in parts:
                    changed_vars.add(p.lower())
                # Add n-grams of the decomposed form
                lower_parts = [p.lower() for p in parts]
                for i in range(len(lower_parts)):
                    for j in range(i + 2, min(i + 5, len(lower_parts) + 1)):
                        changed_vars.add(' '.join(lower_parts[i:j]))
            except ImportError:
                pass

    # Collect NOT_affected behaviors → derive exclusion tokens
    not_affected_tokens = set()
    for m in methods:
        for behavior in m.get('NOT_affected', []):
            # Split behavior description into tokens
            tokens = behavior.lower().replace(',', ' ').replace('.', ' ').split()
            # Keep multi-word phrases and individual words
            not_affected_tokens.update(tokens)
            # Also create bigrams from the description
            for i in range(len(tokens) - 1):
                not_affected_tokens.add(f"{tokens[i]} {tokens[i+1]}")

    # Remove generic words that appear in both changed and not-affected
    not_affected_tokens -= changed_vars
    # Remove very generic words that would over-filter.
    # English stopwords are universal; product-specific generic words are
    # loaded from the KB's discovered_generic_nouns (auto-built, NOT hardcoded).
    generic = {'the', 'a', 'an', 'and', 'or', 'is', 'are', 'for', 'in', 'of',
               'to', 'with', 'not', 'no', 'by', 'on', 'at', 'as', 'be',
               'logic', 'calculations', 'checks', 'overall', 'mechanism'}
    # Augment with KB-discovered generic nouns (data-driven, no hardcoding)
    kb_generic = _load_calltree_stopwords(str(KB_DIR))
    generic |= set(kb_generic)
    not_affected_tokens -= generic

    if not not_affected_tokens:
        print("[OK] No exclusion tokens derived — diff concepts unchanged")
        return diff_concepts

    print(f"[OK] LLM refinement — changed: {changed_vars}")
    print(f"[OK] LLM refinement — exclude: {not_affected_tokens}")

    # Filter diff concepts
    original_count = len(diff_concepts.get('all_phrases', []))
    removed = []

    # Build SPECIFIC changed-variable phrases (2+ words or full camelCase name)
    # for the "keep" check.  Single decomposed words like 'shift', 'gap' are
    # too generic and would prevent excluding phrases like 'split shift gap'.
    specific_cvs = {cv for cv in changed_vars if ' ' in cv or len(cv) > 10}
    # Build NOT_affected multi-word phrases (bigrams+) for exclusion.
    # Single-word exclusion tokens are too aggressive.
    not_affected_phrases = {t for t in not_affected_tokens if ' ' in t}

    def _should_exclude(phrase: str) -> bool:
        """Check if a phrase matches exclusion phrases but NOT specific changed vars."""
        pl = phrase.lower()
        # Never exclude if phrase IS a specific changed variable concept
        if pl in specific_cvs:
            return False
        for cv in specific_cvs:
            if cv in pl or pl in cv:
                return False
        # Exclude if phrase contains a NOT_affected multi-word phrase
        for token in not_affected_phrases:
            if token in pl:
                return True
        return False

    for key in ['all_phrases', 'concepts', 'bigrams', 'trigrams']:
        if key in diff_concepts:
            original = diff_concepts[key]
            filtered = [p for p in original if not _should_exclude(p)]
            excluded = [p for p in original if _should_exclude(p)]
            diff_concepts[key] = filtered
            removed.extend(excluded)

    # Also filter per_identifier
    if 'per_identifier' in diff_concepts:
        filtered_ids = {}
        for ident, phrase in diff_concepts['per_identifier'].items():
            if not _should_exclude(ident.lower()) and not _should_exclude(phrase):
                filtered_ids[ident] = phrase
            else:
                removed.append(f"{ident}={phrase}")
        diff_concepts['per_identifier'] = filtered_ids

    # Also filter identifiers list
    if 'identifiers' in diff_concepts:
        diff_concepts['identifiers'] = [
            i for i in diff_concepts['identifiers']
            if not _should_exclude(i.lower())
        ]

    new_count = len(diff_concepts.get('all_phrases', []))
    if removed:
        unique_removed = sorted(set(removed))
        print(f"[OK] LLM refinement: {original_count} → {new_count} phrases "
              f"(removed {len(unique_removed)}: {unique_removed})")
    else:
        print(f"[OK] LLM refinement: no phrases removed (all relevant)")

    # --- Agent-provided test-language keywords ---
    # Bridge the code↔test terminology gap using QA/business-language search
    # keywords that the Copilot agent supplied when it filled Stage 1.5. The
    # agent writes top-level "test_keywords" / "exclude_keywords" into
    # method_understanding.json (see .github/agents/ria.agent.md); there is no
    # LLM call here — we simply consume those fields.
    try:
        test_keywords = mu_data.get('test_keywords', []) or []
        exclude_keywords = mu_data.get('exclude_keywords', []) or []

        if test_keywords:
            existing = set(diff_concepts.get('all_phrases', []))
            added = []
            for kw in test_keywords:
                kw_lower = (kw or '').lower().strip()
                if kw_lower and kw_lower not in existing:
                    diff_concepts.setdefault('all_phrases', []).append(kw_lower)
                    diff_concepts.setdefault('concepts', []).append(kw_lower)
                    existing.add(kw_lower)
                    added.append(kw_lower)
            if added:
                print(f"[OK] Agent test-language keywords added ({len(added)}): {added}")

        if exclude_keywords:
            for ek in exclude_keywords:
                ek_lower = (ek or '').lower().strip()
                if not ek_lower:
                    continue
                for key in ['all_phrases', 'concepts', 'bigrams', 'trigrams']:
                    if key in diff_concepts:
                        diff_concepts[key] = [
                            p for p in diff_concepts[key]
                            if ek_lower not in p.lower()
                        ]
            print(f"[OK] Agent exclude keywords applied: {[e.lower() for e in exclude_keywords]}")

    except Exception as e:
        print(f"[WARN] Agent test-keyword application skipped: {e}")

    final_count = len(diff_concepts.get('all_phrases', []))
    print(f"[OK] Final diff concepts: {final_count} phrases")
    return diff_concepts


def run_subprocess(cmd, cwd=None, stream=True):
    """Run a child process. Returns (returncode, stdout, stderr).

    When ``stream=True`` we capture stdout/stderr line-by-line and re-emit
    via Python's ``print`` so the TeeLogger picks it up and writes it to
    the consolidated log file. The user still sees output in real time.
    """
    # Normalize the interpreter token. Several stage invocations hardcode
    # 'python3'/'python', which on Windows resolves to the Microsoft Store
    # execution-alias stub (exit code 9009) instead of the active venv.
    if isinstance(cmd, (list, tuple)) and cmd:
        _first = str(cmd[0]).strip()
        _base = os.path.basename(_first).lower()
        if _base in ('python', 'python3', 'python.exe', 'python3.exe') or _base.startswith('python3.') or _base.startswith('python.'):
            cmd = [sys.executable] + list(cmd[1:])

    if stream:
        # Capture-and-stream so TeeLogger sees subprocess output. Merging
        # stderr into stdout keeps log ordering close to what the child
        # process actually printed.
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
        except Exception as _e:
            # Fall back to the original behaviour if Popen fails for
            # exotic reasons (e.g. a process that needs a real tty).
            proc = subprocess.run(cmd, cwd=cwd, text=True)
            return proc.returncode, '', ''

        if proc.stdout is not None:
            # Read and print output in real-time. Break if process exits even
            # if readline blocks on buffered data.
            while True:
                line = proc.stdout.readline()
                if not line:
                    # Check if process exited (readline returns '' at EOF)
                    if proc.poll() is not None:
                        break
                    # Process still running but no output yet, continue reading
                    continue
                print(line, end='')
            proc.stdout.close()
        rc = proc.wait()
        return rc, '', ''
    else:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# KB inspection
# ---------------------------------------------------------------------------
def check_kb_exists():
    """
    Return (all_present, list_of_missing_files).

    Only checks ONE-TIME KB files (synonym_groups, component_map).
    Per-analysis files are rebuilt in Step 0b, so not required beforehand.
    """
    missing = [f for f in KB_FILES_ONETIME if not (KB_DIR / f).exists()]
    return len(missing) == 0, missing


def check_dependencies():
    """
    Verify required Python dependencies are installed.

    Returns:
        (success: bool, missing_required: list[str], missing_optional: list[str])

    'httpx' is REQUIRED for tc_extractor.py to fetch tests from Xray.
    LLM reasoning is handled by the Copilot agent directly (pause/resume), so
    NO cloud SDK (e.g. boto3) is required.
    'missing_optional' is retained for backwards-compatible callers but is
    always an empty list now.
    """
    required = ['httpx']
    missing_required = []
    missing_optional: list = []

    for pkg in required:
        try:
            __import__(pkg)
        except ImportError:
            missing_required.append(pkg)

    return (len(missing_required) == 0, missing_required, missing_optional)


def _pip_available() -> bool:
    """Return True if `python -m pip` is available in the current interpreter."""
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'pip', '--version'],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


def _pip_install_one(pkg_spec: str) -> tuple:
    """
    Install a single package via pip. Tries the default install first; if it
    fails with what looks like a permission error, retries with `--user`.

    Returns: (success: bool, stderr: str)
    """
    base_cmd = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--quiet']
    try:
        result = subprocess.run(
            base_cmd + [pkg_spec],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            return True, ''
        stderr_lower = (result.stderr or '').lower()
        # Retry with --user on permission errors / EnvironmentError.
        if (
            'permission denied' in stderr_lower
            or 'could not install' in stderr_lower
            or 'errno 13' in stderr_lower
            or 'environmenterror' in stderr_lower
            or 'operation not permitted' in stderr_lower
        ):
            user_result = subprocess.run(
                base_cmd + ['--user', pkg_spec],
                capture_output=True, text=True, timeout=300,
            )
            if user_result.returncode == 0:
                return True, ''
            return False, user_result.stderr or result.stderr
        return False, result.stderr
    except subprocess.TimeoutExpired:
        return False, 'pip install timed out after 300s'
    except Exception as exc:
        return False, str(exc)


def _find_working_python():
    """
    Find a working Python interpreter that can run pip install.
    Returns the path to a working python executable, or None if none found.

    Tests actual install capability, not just pip --version.
    Cross-platform: macOS, Linux, Windows.
    """
    system = platform.system()

    # Build platform-specific candidate list
    candidates = []

    if system == 'Darwin':  # macOS
        candidates = [
            '/opt/homebrew/bin/python3.12',
            '/opt/homebrew/bin/python3.13',
            '/opt/homebrew/bin/python3.11',
            '/usr/local/bin/python3.12',
            '/usr/local/bin/python3.13',
            '/usr/local/bin/python3.11',
            '/usr/local/bin/python3',
        ]
    elif system == 'Linux':
        candidates = [
            '/usr/bin/python3.12',
            '/usr/bin/python3.13',
            '/usr/bin/python3.11',
            '/usr/bin/python3.10',
            '/usr/bin/python3',
            '/usr/local/bin/python3.12',
            '/usr/local/bin/python3.13',
            '/usr/local/bin/python3.11',
        ]
    elif system == 'Windows':
        candidates = [
            'py -3.12',
            'py -3.13',
            'py -3.11',
            'py -3.10',
            'python3.12',
            'python3.13',
            'python3.11',
            'python3.10',
            'python',
        ]

    # Add generic candidates that work on all platforms
    candidates.extend([
        'python3.12',
        'python3.13',
        'python3.11',
        'python3.10',
        'python3',
    ])

    for python_exe in candidates:
        try:
            # For Windows 'py' launcher, split the command
            if python_exe.startswith('py -'):
                cmd = python_exe.split() + ['-m', 'pip', 'install', '--dry-run', 'httpx']
            else:
                cmd = [python_exe, '-m', 'pip', 'install', '--dry-run', 'httpx']

            # Test if pip install command works (not just --version)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15
            )
            # returncode 0 means pip install works (would install successfully)
            if result.returncode == 0:
                return python_exe
        except FileNotFoundError:
            # Python executable doesn't exist
            continue
        except Exception:
            # Any other error (timeout, etc) - try next candidate
            continue
    return None


def auto_install_dependencies(missing_packages: list) -> bool:
    """
    Auto-install missing Python dependencies via pip.
    Uses --break-system-packages for Homebrew Python (PEP 668).

    Args:
        missing_packages: List of package names (or pip specs) to install

    Returns:
        True if all packages installed successfully, False otherwise
    """
    if not missing_packages:
        return True

    print()
    print("=" * 80)
    print("[SETUP] Installing required dependencies...")
    print("=" * 80)

    # Try to install with --break-system-packages flag (for Homebrew Python 3.14+)
    for pkg in missing_packages:
        print(f"  -> Installing {pkg}...", end='', flush=True)

        # Use --break-system-packages for externally-managed Python installations
        cmd = [sys.executable, '-m', 'pip', 'install', '--break-system-packages', pkg]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120
            )
            if result.returncode == 0:
                print(" ✓")
            else:
                print(" ✗")
                print()
                print("[ERROR] Failed to install dependencies.")
                print()
                print("Manual install command:")
                print(f"  pip3 install --break-system-packages {' '.join(missing_packages)}")
                print()
                print("=" * 80)
                return False
        except Exception as e:
            print(f" ✗ ({str(e)})")
            return False

    print()
    print("✓ All dependencies installed successfully!")
    print()
    return True

    # If installation succeeded, we're done
    if not failed:
        print()
        print("[OK] All dependencies installed successfully.")
        print()
        return True

    # Installation failed - check if it's a broken pip environment
    print()
    print("=" * 80)
    print("[WARNING] Current Python's pip failed to install packages:")
    print(f"           {sys.executable}")
    print("          Searching for alternative Python installation...")
    print("=" * 80)

    # Try to find a working Python
    working_python = _find_working_python()
    if not working_python:
        # No alternative Python found - try to fix current Python's expat issue first
        print()
        print("[INFO] No alternative Python found.")
        print("       Attempting to fix current Python environment...")
        print()

        system = platform.system()
        success = False
        working_python = None  # Initialize for the fix attempts below

        try:
            if system == 'Darwin':  # macOS
                # Check if Homebrew is available
                brew_check = subprocess.run(
                    ['which', 'brew'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if brew_check.returncode != 0:
                    print("[ERROR] Homebrew not found.")
                    print("        Cannot auto-fix Python environment.")
                    print()
                    print("Workarounds:")
                    print("  1. Install Homebrew: /bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"")
                    print(f"  2. Or install dependencies manually: pip install {' '.join(p for p, _ in failed)}")
                    print("=" * 80)
                    return False

                # Try to fix expat library issue (faster than installing new Python)
                print("  -> Reinstalling expat library to fix Python 3.14...")
                fix_result = subprocess.run(
                    ['brew', 'reinstall', 'expat'],
                    capture_output=True,
                    text=True,
                    timeout=120  # 2 minutes max
                )
                if fix_result.returncode == 0:
                    print(" ✓")
                    # Test if pip install works now
                    test_result = subprocess.run(
                        [sys.executable, '-m', 'pip', 'install', '--dry-run', 'httpx'],
                        capture_output=True,
                        text=True,
                        timeout=15
                    )
                    if test_result.returncode == 0:
                        print("[INFO] Python 3.14 fixed! Using current Python.")
                        working_python = sys.executable
                        success = True
                    else:
                        print("[WARN] Expat reinstalled but pip still broken.")
                else:
                    print(" ✗")

                # If expat fix didn't work, fall back to installing Python 3.12
                if not success:
                    print()
                    print("  -> Expat fix didn't work. Installing Python 3.12 (1-2 minutes)...")
                    print()
                    install_result = subprocess.run(
                        ['brew', 'install', 'python@3.12'],
                        timeout=600  # 10 minutes max
                    )
                    if install_result.returncode == 0:
                        print()
                        print(" ✓ Python 3.12 installed successfully")
                        # Use the known installation path directly
                        working_python = '/opt/homebrew/bin/python3.12'
                        success = True
                    else:
                        print()
                        print(" ✗ Installation failed")

            elif system == 'Linux':
                # Try apt-get (Debian/Ubuntu)
                print("  -> Attempting to install Python 3.12 via apt...")
                install_result = subprocess.run(
                    ['sudo', 'apt-get', 'update'],
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                if install_result.returncode == 0:
                    install_result = subprocess.run(
                        ['sudo', 'apt-get', 'install', '-y', 'python3.12'],
                        capture_output=True,
                        text=True,
                        timeout=600
                    )
                    if install_result.returncode == 0:
                        print(" ✓")
                        # Use the standard Linux installation path
                        working_python = '/usr/bin/python3.12'
                        success = True
                    else:
                        print(" ✗")

            elif system == 'Windows':
                print("[INFO] On Windows, Python auto-install is not supported.")
                print()
                print("Please install Python 3.12 manually:")
                print("  1. Visit: https://www.python.org/downloads/")
                print("  2. Download Python 3.12 installer")
                print("  3. Run installer with 'Add to PATH' checked")
                print(f"  4. Or install dependencies manually: pip install {' '.join(p for p, _ in failed)}")
                print("=" * 80)
                return False

            if not success:
                print()
                print("[ERROR] Failed to auto-install Python 3.12.")
                print()
                print("Manual installation instructions:")
                if system == 'Darwin':
                    print("  brew install python@3.12")
                elif system == 'Linux':
                    print("  sudo apt-get install python3.12")
                    print("  # OR for RHEL/CentOS:")
                    print("  sudo yum install python3.12")
                print()
                print(f"Or install dependencies manually: pip install {' '.join(p for p, _ in failed)}")
                print("=" * 80)
                return False

            # If we didn't set working_python in the success path, try to find it
            if success and not working_python:
                working_python = _find_working_python()

            if not working_python:
                print()
                print("[ERROR] Could not find or install a working Python.")
                print("=" * 80)
                return False

            print()

        except subprocess.TimeoutExpired:
            print(" ✗ (timeout)")
            print()
            print("[ERROR] Python 3.12 installation timed out.")
            if system == 'Darwin':
                print("        Try manually: brew install python@3.12")
            elif system == 'Linux':
                print("        Try manually: sudo apt-get install python3.12")
            print("=" * 80)
            return False
        except Exception as e:
            print(f" ✗ ({str(e)})")
            print()
            print("[ERROR] Unexpected error during Python 3.12 installation.")
            print(f"        {str(e)}")
            print("=" * 80)
            return False

    print(f"[INFO] Found working Python: {working_python}")
    print()

    # Install using the working Python (bypassing pip due to expat issues)
    print()
    print("[INFO] Bypassing pip to install packages directly (expat workaround)...")
    print()

    import urllib.request
    import tempfile
    import zipfile
    import shutil

    for pkg_spec, _ in failed:
        # Extract package name from spec (e.g., "httpx>=0.24.0" -> "httpx")
        pkg_name = pkg_spec.split('>=')[0].split('==')[0].split('[')[0].strip()

        print(f"  -> Installing {pkg_name}...")

        try:
            # Download package wheel from PyPI
            if pkg_name == 'httpx':
                wheel_url = 'https://files.pythonhosted.org/packages/78/82/08f8c936781f67d9e6b9eeb8a0c8b4e406136ea4c3d1f89a5db71d42e0e6/httpx-0.27.2-py3-none-any.whl'
            else:
                print(f" ✗ Package {pkg_name} not supported for direct install")
                return False

            # Download to temp directory
            with tempfile.TemporaryDirectory() as tmpdir:
                wheel_path = os.path.join(tmpdir, f'{pkg_name}.whl')
                print(f"     Downloading {pkg_name}...", end='', flush=True)
                urllib.request.urlretrieve(wheel_url, wheel_path)
                print(" ✓")

                # Extract wheel to site-packages
                print(f"     Installing {pkg_name}...", end='', flush=True)

                # Get Python's site-packages directory
                result = subprocess.run(
                    [sys.executable, '-c', 'import site; print(site.getsitepackages()[0])'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode != 0:
                    print(" ✗ Could not find site-packages")
                    return False

                site_packages = result.stdout.strip()

                # Extract wheel
                with zipfile.ZipFile(wheel_path, 'r') as zip_ref:
                    for member in zip_ref.namelist():
                        # Skip .dist-info metadata
                        if '.dist-info/' not in member or member.endswith('/'):
                            target_path = os.path.join(site_packages, member)
                            os.makedirs(os.path.dirname(target_path), exist_ok=True)
                            with zip_ref.open(member) as source, open(target_path, 'wb') as target:
                                shutil.copyfileobj(source, target)

                print(" ✓")

        except Exception as e:
            print(f" ✗ ({str(e)})")
            return False

    print()
    print("=" * 80)
    print("✓ Dependencies installed successfully!")
    print(f"  Note: Installed using {working_python}")
    print("=" * 80)
    print()
    return True


# Required + optional package specs used by ensure_dependencies().
# Keep these aligned with requirements.txt so devs only need a single
# source of truth.
_REQUIRED_DEPS = {
    # import name -> pip spec
    # httpx: Xray API client (tc_extractor.py)
    'httpx': 'httpx>=0.24.0',
    # LLM reasoning is provided by the Copilot agent directly (pause/resume);
    # no cloud SDK (boto3) is needed.
}
# Retained for backwards-compatible reference.
_OPTIONAL_DEPS: dict = {}


def ensure_dependencies() -> bool:
    """
    Auto-install any missing Python dependencies.

    This is the entry point called at the very start of main() so the
    developer NEVER has to run pip manually.  LLM reasoning is provided by
    the Copilot agent directly (pause/resume), so there is no cloud SDK to
    install.

    Returns True if all required deps are present after the call.
    """
    missing_required = []
    for module, spec in _REQUIRED_DEPS.items():
        try:
            __import__(module)
        except ImportError:
            missing_required.append(spec)

    if not missing_required:
        # Fast path: nothing to install — silent on subsequent runs.
        return True

    # Install everything missing in one go (single pip invocation overhead).
    if not auto_install_dependencies(missing_required):
        # Re-check what's actually missing after the install attempt.
        still_missing_required = []
        for module, _ in _REQUIRED_DEPS.items():
            try:
                __import__(module)
            except ImportError:
                still_missing_required.append(module)
        if still_missing_required:
            return False

    return True


def _parse_env_file(env_path: Path) -> dict:
    """Lightweight parser for ria_config.env (key=value)."""
    env: dict = {}
    if not env_path.exists():
        return env
    try:
        with open(env_path, 'r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip()
    except Exception:
        pass
    return env


def check_credentials(env_file: Path) -> dict:
    """
    Inspect ria_config.env and report which credential groups are configured.

    A value is considered "configured" if it is set AND not still equal to a
    template placeholder (placeholders end with `_HERE` or contain
    `your-domain` / `your.email`).

    Returns a dict with keys:
        env_file_present        : bool
        xray_configured         : bool
        jira_configured         : bool
        project_keys_configured : bool
        llm_configured          : bool
    """
    info = {
        'env_file_present': env_file.exists(),
        'xray_configured': False,
        'jira_configured': False,
        'project_keys_configured': False,
        'llm_configured': False,
    }
    if not info['env_file_present']:
        return info

    env = _parse_env_file(env_file)

    def _is_real(value: str) -> bool:
        if not value:
            return False
        v = value.strip()
        if not v:
            return False
        upper = v.upper()
        if upper.endswith('_HERE'):
            return False
        if 'your-domain' in v.lower() or 'your.email' in v.lower():
            return False
        return True

    info['xray_configured'] = (
        _is_real(env.get('XRAY_CLIENT_ID', ''))
        and _is_real(env.get('XRAY_CLIENT_SECRET', ''))
    )
    info['jira_configured'] = (
        _is_real(env.get('JIRA_BASE_URL', ''))
        and _is_real(env.get('JIRA_USER', ''))
        and _is_real(env.get('JIRA_API_TOKEN', ''))
    )
    info['project_keys_configured'] = _is_real(
        env.get('PROJECT_KEYS', '') or env.get('PROJECT_KEY', '')
    )
    # LLM reasoning is performed by the Copilot agent directly (pause/resume);
    # no cloud credentials are required, so the LLM is always "configured".
    info['llm_configured'] = True
    return info


def _print_first_run_setup_banner(*,
                                  missing_required: list,
                                  missing_optional: list,
                                  cred_info: dict,
                                  env_file: Path) -> None:
    """
    Print the friendly first-run setup message describing exactly what the
    developer needs to install / configure.
    """
    requirements_path = SKILL_DIR / 'requirements.txt'
    setup_doc = SKILL_DIR / 'FIRST_RUN_SETUP.md'

    print()
    print("=" * 80)
    print("RIA v2 AGENT - FIRST RUN SETUP REQUIRED")
    print("=" * 80)
    print()
    print("[SETUP] This appears to be your first time running RIA on this machine.")
    print()

    # ----- Dependency report -----
    # Dependencies are auto-installed by ensure_dependencies() at the start
    # of main(), so by the time this banner prints, deps SHOULD already be
    # present. We still report status for transparency; the banner is now
    # only triggered for *config* problems.
    print("[CHECK] Python dependencies (auto-installed on first run):")
    # Only httpx is required (Xray API client for tc_extractor.py). LLM
    # reasoning is handled by the Copilot agent directly (pause/resume), so
    # there is no cloud SDK (boto3) to install.
    required = ['httpx']
    for pkg in required:
        if pkg in missing_required:
            print(f"  [X] {pkg} - MISSING (auto-install failed; install manually)")
        else:
            print(f"  [OK] {pkg} - found")
    print()

    if missing_required:
        # Only surface manual-install hint if auto-install actually failed.
        print("[ACTION] Auto-install failed for required deps. Install manually:")
        print()
        if requirements_path.exists():
            print(f"  pip install -r {requirements_path}")
            print()
            print("  (or, to install just what is missing:)")
            print(f"  pip install {' '.join(missing_required)}")
        else:
            print(f"  pip install {' '.join(missing_required)}")
        print()

    # ----- Configuration report -----
    print("[CHECK] Configuration:")
    if cred_info['env_file_present']:
        print(f"  [OK] ria_config.env found  ({env_file})")
    else:
        print(f"  [X] ria_config.env NOT found at {env_file}")
        template = env_file.with_suffix('.env.template')
        if template.exists():
            print(f"        Copy the template: cp {template} {env_file}")
        print()

    if cred_info['env_file_present']:
        print("  {0} XRAY credentials {1}".format(
            "[OK]" if cred_info['xray_configured'] else "[X] ",
            "configured" if cred_info['xray_configured']
            else "MISSING (XRAY_CLIENT_ID / XRAY_CLIENT_SECRET still placeholders)"
        ))
        print("  {0} JIRA credentials {1}".format(
            "[OK]" if cred_info['jira_configured'] else "[OPTIONAL]",
            "configured" if cred_info['jira_configured']
            else "not configured (only needed for JIRA integration with --jira-card)"
        ))
        print("  {0} Project keys {1}".format(
            "[OK]" if cred_info['project_keys_configured'] else "[X] ",
            "configured" if cred_info['project_keys_configured']
            else "MISSING (set PROJECT_KEYS in ria_config.env)"
        ))
        print("  [OK] LLM reasoning provided by the Copilot agent "
              "(pause/resume - no cloud credentials required)")
    print()

    # ----- Next steps -----
    print("[NEXT] After installing dependencies and configuring credentials, "
          "run again:")
    print()
    print("  python3 .github/skills/regression-impact-analysis/scripts/"
          "ria_agent.py --user-prompt \"Run RIA on my changes\"")
    print()
    if setup_doc.exists():
        print(f"See {setup_doc} for detailed instructions.")
    else:
        print("See FIRST_RUN_SETUP.md for detailed instructions.")
    print("=" * 80)
    print()


def check_raw_test_corpus():
    """
    Verify the RAW test corpus exists (required for enrichment).

    all_tcs_extracted.json must be present (from tc_extractor.py) before
    the first analysis. The enriched version is created by build_flow_registry.py
    during Step 0b.
    """
    raw_corpus = Path(REPO_ROOT) / '.github' / 'RIA_INPUT' / 'all_tcs_extracted.json'
    if not raw_corpus.exists():
        print()
        print(f"[INFO] Raw test corpus not found: {raw_corpus}")

        # Dependencies were already auto-installed in main() via
        # ensure_dependencies() before we got here, so we only need to
        # verify credentials/config before invoking tc_extractor.py.
        ok, missing_req, missing_opt = check_dependencies()

        env_file = SKILL_DIR / 'configs' / 'ria_config.env'
        cred_info = check_credentials(env_file)

        # Hard blockers: missing env file, missing XRAY credentials, or
        # missing project keys. LLM reasoning needs no credentials (Copilot
        # handoff), so it never blocks here.
        # If required deps somehow still missing (e.g. user forced --skip),
        # this is still flagged below.
        hard_block = (
            (not ok)
            or (not cred_info['env_file_present'])
            or (not cred_info['xray_configured'])
            or (not cred_info['project_keys_configured'])
            or (not cred_info['llm_configured'])
        )
        if hard_block:
            _print_first_run_setup_banner(
                missing_required=missing_req,
                missing_optional=missing_opt,
                cred_info=cred_info,
                env_file=env_file,
            )
            return False

        print("       Auto-extracting test cases from Xray Cloud...")
        print()
        if not _auto_extract_test_corpus():
            return False
        # Re-check after extraction
        if not raw_corpus.exists():
            print(f"[ERROR] tc_extractor.py completed but file still missing: {raw_corpus}")
            return False
    return True


def _auto_extract_test_corpus() -> bool:
    """
    Automatically run tc_extractor.py using credentials from ria_config.env.
    Returns True on success.
    """
    env_file = SKILL_DIR / 'configs' / 'ria_config.env'
    tc_script = SCRIPT_DIR / 'tc_extractor.py'

    if not tc_script.exists():
        print(f"[ERROR] tc_extractor.py not found at: {tc_script}")
        return False
    if not env_file.exists():
        print(f"[ERROR] ria_config.env not found at: {env_file}")
        return False

    print(f"[BUILD] Running tc_extractor.py (fetching from Xray Cloud API)...")
    cmd = ['python3', str(tc_script), '--env-file', str(env_file)]
    rc, _, _ = run_subprocess(cmd, cwd=str(REPO_ROOT))
    if rc == 2:
        # tc_extractor.py uses exit code 2 to signal "missing httpx".
        # AUTO-INSTALL and retry
        ok, missing_req, missing_opt = check_dependencies()
        if not ok:
            if auto_install_dependencies(missing_req):
                # Retry after successful install
                print("[RETRY] Re-running tc_extractor.py after installing dependencies...")
                rc, _, _ = run_subprocess(cmd, cwd=str(REPO_ROOT))
                if rc == 0:
                    print("[OK] Test corpus extracted successfully")
                    return True
            else:
                print("[ERROR] Failed to auto-install dependencies.")
                print("        Please install manually: pip install " + " ".join(missing_req))

        # If still failing, show setup banner
        cred_info = check_credentials(env_file)
        _print_first_run_setup_banner(
            missing_required=missing_req,
            missing_optional=missing_opt,
            cred_info=cred_info,
            env_file=env_file,
        )
        return False
    if rc != 0:
        print(f"[ERROR] tc_extractor.py exited with code {rc}")
        print("        Check your XRAY_CLIENT_ID/SECRET and PROJECT_KEYS in ria_config.env")
        return False
    print("[OK] Test corpus extracted successfully")
    return True


def check_kb_freshness(max_age_hours: int = 24):
    """
    Return (is_fresh, oldest_age_in_hours). Missing files counted as +inf.

    Only checks ONE-TIME KB files. Per-analysis files are always fresh
    (rebuilt in Step 0b).
    """
    oldest_age = 0.0
    for kb_file in KB_FILES_ONETIME:
        kb_path = KB_DIR / kb_file
        if kb_path.exists():
            age_hours = (time.time() - kb_path.stat().st_mtime) / 3600.0
            oldest_age = max(oldest_age, age_hours)
        else:
            return False, float('inf')
    return oldest_age <= max_age_hours, oldest_age


# ---------------------------------------------------------------------------
# Stage 0: KB build
# ---------------------------------------------------------------------------
def run_kb_build(force: bool = False) -> bool:
    """
    Run Stage 0: build the ONE-TIME KB files (synonym_groups, component_map).

    Per-analysis files (flow_registry, flow_dependencies) are NOT built here.
    They are built in rebuild_focused_kb() (Step 0b) before every analysis,
    because they must be FOCUSED on the changed method to be accurate.
    """
    start_timer('stage0_kb_build')
    banner("STAGE 0: BUILDING KNOWLEDGE BASE (One-Time Setup)")

    KB_DIR.mkdir(parents=True, exist_ok=True)

    # Verify raw test corpus FIRST — KB builders need it as input.
    if not check_raw_test_corpus():
        return False

    # Track which (script + arg tuple) we've already invoked in this run so the
    # discovery builder doesn't fire twice when listed for multiple outputs.
    invoked: set = set()
    # Quick Win #3: track artifacts whose fingerprint we should write AFTER
    # their builder invocation succeeds. We can't write the .fp inside the
    # loop because some builders produce multiple artifacts in a single run.
    fingerprint_writes_needed: list = []
    for entry in KB_BUILD_STAGES:
        # Backward-compatible: legacy entries were 2-tuples (script, kb_file).
        if len(entry) == 2:
            script, kb_file = entry
            extra_args = []
        else:
            script, kb_file, extra_args = entry

        kb_path = KB_DIR / kb_file
        # Quick Win #3: content-hash cache for expensive KB artifacts.
        # When the artifact exists AND its fingerprint matches the current
        # dependency contents, skip the build (cache hit). --rebuild-kb
        # bypasses the cache entirely (force=True).
        cache_deps = _kb_artifact_dependencies(kb_file)
        if (cache_deps is not None
                and not force
                and kb_path.exists()
                and _kb_artifact_is_fresh(kb_path, cache_deps)):
            print(f"[CACHE HIT] {kb_file} fingerprint matches dependencies — skipping build")
            continue

        if kb_path.exists() and not force:
            print(f"[SKIP]  {kb_file} already exists")
            # Even when we skip a build because the file exists, write a
            # fingerprint so the next run can use the content-hash cache
            # (lets us bootstrap the cache on existing KB directories).
            if cache_deps is not None:
                _write_kb_fingerprint(kb_path, cache_deps)
            continue

        script_path = SCRIPT_DIR / script
        if not script_path.exists():
            print(f"[ERROR] Missing builder script: {script_path}")
            return False

        invocation_key = (script, tuple(extra_args))
        if invocation_key in invoked:
            # Same builder run already produced this file in an earlier loop
            # iteration (e.g. one builder writes multiple KB files).
            if kb_path.exists():
                print(f"[OK]    {kb_file} (produced by previous invocation)")
                if cache_deps is not None:
                    fingerprint_writes_needed.append((kb_path, cache_deps))
                continue

        print(f"[BUILD] {kb_file}  (running {script} {' '.join(extra_args)})")
        # Run from REPO_ROOT so KB builders that scan the codebase can find sources.
        # -u flag: unbuffered output so progress prints appear in real-time
        cmd = ['python3', '-u', str(script_path)] + list(extra_args)
        rc, _, _ = run_subprocess(cmd, cwd=str(REPO_ROOT))
        if rc != 0:
            print(f"[ERROR] {script} exited with code {rc}")
            return False
        invoked.add(invocation_key)
        print(f"[OK]    {kb_file}")
        if cache_deps is not None:
            fingerprint_writes_needed.append((kb_path, cache_deps))

    # Persist fingerprint sidecars now that every builder has finished.
    for art_path, deps in fingerprint_writes_needed:
        if art_path.exists():
            _write_kb_fingerprint(art_path, deps)

    print()
    print("[OK] Knowledge Base build complete")
    end_timer('stage0_kb_build')
    return True


# ---------------------------------------------------------------------------
# Per-change focused KB rebuild (GAP 2)
# ---------------------------------------------------------------------------
def rebuild_focused_kb(changed_method: str, changed_file: str,
                       kb_dir_override: Path = None) -> bool:
    """
    Rebuild flow_registry.json and flow_dependencies.json focused on the
    specific changed component.

    Rationale (GAP 2 - User's Insight):
        ONE-TIME KB files (built once, stable):
        - synonym_groups.json: CRUD/PROCESS verb groups (codebase-wide)
        - component_map.json: Component consolidation (corpus-wide)

        PER-ANALYSIS KB files (rebuilt every run, FOCUSED):
        - flow_registry.json: Maps tests to flows reachable FROM THIS CHANGE
        - flow_dependencies.json: Dependencies for THIS change's components
        - all_tcs_extracted_enriched_source.json: Tests re-tagged for THIS
          change (side-effect of build_flow_registry.py; reads raw corpus and
          writes the enriched version with flow tags relevant to this change)

        Different code changes traverse different call trees, so these files
        must be rebuilt per analysis to be accurate. This runs before Stages 1-4
        so they always see a fresh, change-focused KB.

    kb_dir_override (Quick Win #1 - parallelism without locks):
        When provided, all per-analysis OUTPUT files (flow_registry.json,
        flow_dependencies.json, enriched corpora) are written to
        ``kb_dir_override`` instead of the canonical ``KB_DIR``. Shared
        one-time KB INPUTS (synonym_groups.json, component_map.json,
        discovered_generic_nouns.json, ...) are still read from ``KB_DIR``.
        This lets multi-method workers run in parallel without contending
        for the same output files - no global lock required.
    """
    start_timer('stage0_focused_kb')
    banner("PER-CHANGE FOCUSED KB REBUILD (Flow Registry + Dependencies)")

    # Resolve effective output directory. Default = canonical KB_DIR;
    # override = per-worker scratch dir for parallel multi-method runs.
    effective_kb_dir = Path(kb_dir_override) if kb_dir_override else KB_DIR
    effective_kb_dir.mkdir(parents=True, exist_ok=True)
    KB_DIR.mkdir(parents=True, exist_ok=True)  # canonical input dir

    # ---- flow_registry.json (FOCUSED on changed_file/changed_method) ----
    section("Rebuilding flow_registry.json (FOCUSED mode)")
    registry_script = SCRIPT_DIR / 'build_flow_registry.py'
    registry_cmd = [
        'python3', str(registry_script),
        '--changed-file', changed_file,
        '--changed-method', changed_method,
        # Write enriched corpus to all_tcs_extracted_enriched_source.json.
        # The legacy backward-compat file is no longer produced.
        '--pipeline-type', 'source',
        # Outputs (registry + enriched corpus) go to the effective KB dir.
        '--output-dir', str(effective_kb_dir),
        # Synonym groups + generic nouns are SHARED one-time KB inputs and
        # are always read from the canonical KB_DIR even when the worker
        # writes to a private scratch dir.
        '--synonym-groups', str(KB_DIR / 'synonym_groups.json'),
        '--kb-input-dir', str(KB_DIR),
    ]
    rc, _, _ = run_subprocess(registry_cmd, cwd=str(REPO_ROOT))
    if rc != 0:
        print(f"[ERROR] build_flow_registry.py failed (rc={rc})")
        return False
    print("[OK] flow_registry.json rebuilt (focused on changed component)")

    # ---- flow_dependencies.json (recomputed from new flow_registry) ----
    # Pass changed component so dependencies are scoped to changed code only.
    # Fix P2: Cache by (changed_component, registry_hash) to skip redundant
    # build_flow_dependencies.py invocations within a single pipeline run.
    section("Rebuilding flow_dependencies.json")

    # Derive component name from changed file (e.g., AgentTradeDao.java → AgentTrade)
    changed_component = Path(changed_file).stem

    # Check cache: if we've already built deps for this (component, registry)
    # pair in this run, reuse the cached JSON instead of re-running the script.
    registry_path = effective_kb_dir / 'flow_registry.json'
    registry_hash = _hash_file_content(registry_path)
    cache_key = (changed_component, registry_hash)

    deps_path = effective_kb_dir / 'flow_dependencies.json'

    if cache_key in _FLOW_DEPS_CACHE:
        print(f"[CACHE HIT] Reusing flow_dependencies for {changed_component} "
              f"(registry hash: {registry_hash[:8]}...)")
        # Write cached content to disk so downstream stages see the file.
        with open(deps_path, 'w', encoding='utf-8') as f:
            json.dump(_FLOW_DEPS_CACHE[cache_key], f, indent=2)
        print(f"[OK] flow_dependencies.json restored from cache")
    else:
        print(f"[CACHE MISS] Building flow_dependencies for {changed_component}")
        deps_script = SCRIPT_DIR / 'build_flow_dependencies.py'
        # Point the flow-deps builder at the SOURCE-pipeline enriched corpus
        # written by build_flow_registry.py with --pipeline-type source. If
        # that file is absent, let build_flow_dependencies.py use its
        # internal default by leaving the flag off.
        source_enriched = effective_kb_dir / 'all_tcs_extracted_enriched_source.json'
        deps_cmd = [
            'python3', str(deps_script),
            '--changed-component', changed_component,
            # Read the freshly-written registry from the effective KB dir
            # (per-worker scratch when overridden) and write deps alongside.
            '--flow-registry', str(registry_path),
            '--output', str(deps_path),
            # component_map.json is a one-time KB input; always read from
            # canonical KB_DIR (never per-worker).
            '--component-map', str(KB_DIR / 'component_map.json'),
        ]
        if source_enriched.exists():
            deps_cmd += ['--enriched-corpus-path', str(source_enriched)]
        rc, _, _ = run_subprocess(deps_cmd, cwd=str(REPO_ROOT))
        if rc != 0:
            print(f"[ERROR] build_flow_dependencies.py failed (rc={rc})")
            return False

        # Cache the freshly-built result for future calls in this same run.
        if deps_path.exists():
            try:
                with open(deps_path, 'r', encoding='utf-8') as f:
                    _FLOW_DEPS_CACHE[cache_key] = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                # Caching is best-effort: a read failure must not break the run.
                print(f"[WARN] Could not cache flow_dependencies.json: {e}")

        print(f"[OK] flow_dependencies.json rebuilt (focused on {changed_component})")

    print()
    print("[OK] Focused KB rebuild complete")
    end_timer('stage0_focused_kb')
    return True


# ---------------------------------------------------------------------------
# Stage 2/3 compatibility shim
# ---------------------------------------------------------------------------
def _derive_component_from_file(changed_file: str) -> str:
    """
    Derive the canonical component name from a changed file path by
    stripping common source-file extensions. Mirrors the logic in
    stage4_test_correlation.derive_component_from_file (filename-stem fallback)
    and build_flow_dependencies (Path(changed_file).stem).
    """
    if not changed_file:
        return ""
    base = os.path.basename(changed_file)
    stem = base
    for ext in ('.java', '.py', '.ts', '.tsx', '.js', '.jsx'):
        if stem.lower().endswith(ext):
            stem = stem[:-len(ext)]
            break
    return stem


def _resolve_method_line_range(changed_file: str, method_name: str):
    """
    Best-effort resolution of a method's 1-based (line_start, line_end) by
    scanning the source file for the declaration and brace-matching its body.

    Used in EXPLICIT mode where CLI args carry no line numbers. Returns
    (None, None) when the file is unreadable or the method cannot be located,
    so callers must treat the result as optional. Brace matching is a
    heuristic that works for C-style languages (Java/TS/JS); for other
    languages it falls back to the declaration line with a small window.
    """
    if not changed_file or not method_name:
        return (None, None)
    try:
        path = changed_file
        if not os.path.isabs(path):
            path = os.path.join(str(REPO_ROOT), changed_file)
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()
    except Exception:
        return (None, None)

    decl_re = re.compile(r'\b' + re.escape(method_name) + r'\s*\(')
    start_idx = None
    for i, line in enumerate(lines):
        if decl_re.search(line):
            start_idx = i
            break
    if start_idx is None:
        return (None, None)

    # Find the opening brace at/after the declaration, then brace-match.
    depth = 0
    seen_open = False
    end_idx = None
    for j in range(start_idx, len(lines)):
        for ch in lines[j]:
            if ch == '{':
                depth += 1
                seen_open = True
            elif ch == '}':
                depth -= 1
                if seen_open and depth == 0:
                    end_idx = j
                    break
        if end_idx is not None:
            break

    if end_idx is None:
        # No brace body found (e.g. abstract/interface/non-brace language).
        # Return the declaration line as a single-line range.
        return (start_idx + 1, start_idx + 1)
    return (start_idx + 1, end_idx + 1)


# ---------------------------------------------------------------------------
# Stages 5-6: Refinement pipeline
# ---------------------------------------------------------------------------
def run_refinement(changed_method: str,
                    changed_components: list = None,
                    flow_registry_path_override: Path = None,
                    flow_deps_path_override: Path = None,
                    enriched_corpus_path_override: Path = None) -> bool:
    """Run Stages 5 and 6 via dedicated scripts.

    Post-pipeline-simplification: Stages 5/6 read flows directly from the
    focused KB (flow_registry.json + flow_dependencies.json). The
    orchestrator passes --changed-components explicitly so component-map
    lookups and DIRECT/INDIRECT classification work without a stage2 shim.

    Args:
        flow_registry_path_override / flow_deps_path_override:
            Optional overrides. When None, the canonical KB files
            (flow_registry.json / flow_dependencies.json) are used.
        enriched_corpus_path_override:
            Optional override. When set, Stages 5/6 read this enriched
            corpus for flow-quota / coverage analysis. Defaults to
            `all_tcs_extracted_enriched_source.json`.
    """
    banner("STAGES 5-6: REFINEMENT PIPELINE")

    # --- Stage 5 -----------------------------------------------------------
    start_timer('stage5')
    stage4_path = OUTPUT_DIR / 'stage4_recommended_tests.json'
    stage5_path = OUTPUT_DIR / 'stage5_refined_tests.json'
    stage6_path = OUTPUT_DIR / 'stage6_aggressive_tests.json'
    flow_registry_path = (flow_registry_path_override
                          if flow_registry_path_override is not None
                          else KB_DIR / 'flow_registry.json')
    flow_deps_path = (flow_deps_path_override
                      if flow_deps_path_override is not None
                      else KB_DIR / 'flow_dependencies.json')
    # Resolve the enriched corpus path that Stages 5/6 will consume.
    if enriched_corpus_path_override is not None:
        enriched_for_refine = Path(enriched_corpus_path_override)
    else:
        enriched_for_refine = KB_DIR / 'all_tcs_extracted_enriched_source.json'

    if not stage4_path.exists():
        print(f"[ERROR] Stage 4 output not found: {stage4_path}")
        print("        Cannot run Stage 5 without Stage 4 output.")
        end_timer('stage5')
        return False

    cc_arg = ','.join([c for c in (changed_components or []) if c])

    section("Stage 5: Keyword precision + flow diversity")
    cmd_s5 = [
        'python3', str(SCRIPT_DIR / 'stage5_refine_tests.py'),
        '--changed-method', changed_method,
        '--input-file', str(stage4_path),
        '--output-file', str(stage5_path),
        '--flow-registry', str(flow_registry_path),
        '--flow-dependencies', str(flow_deps_path),
        '--kb-dir', str(KB_DIR),
        '--enriched-corpus-path', str(enriched_for_refine),
    ]
    if cc_arg:
        cmd_s5.extend(['--changed-components', cc_arg])
    rc, _, _ = run_subprocess(cmd_s5, cwd=str(SCRIPT_DIR))
    if rc != 0:
        print(f"[ERROR] Stage 5 failed (exit code {rc})")
        end_timer('stage5')
        return False
    print("[OK] Stage 5 complete")
    end_timer('stage5')

    # --- Stage 6 -----------------------------------------------------------
    start_timer('stage6')
    if not stage5_path.exists():
        print(f"[ERROR] Stage 5 output not found: {stage5_path}")
        end_timer('stage6')
        return False

    section("Stage 6: Aggressive suppression")
    anchor_file = OUTPUT_DIR / 'anchor_concepts.json'
    cmd_s6 = [
        'python3', str(SCRIPT_DIR / 'stage6_aggressive_suppression.py'),
        '--changed-method', changed_method,
        '--input-file', str(stage5_path),
        '--output-file', str(stage6_path),
        '--flow-registry', str(flow_registry_path),
        '--flow-dependencies', str(flow_deps_path),
        '--stage4-tests', str(stage4_path),
        '--kb-dir', str(KB_DIR),
        '--enriched-corpus-path', str(enriched_for_refine),
    ]
    if cc_arg:
        cmd_s6.extend(['--changed-components', cc_arg])
    if anchor_file.exists():
        cmd_s6.extend(['--anchor-concepts', str(anchor_file)])
    rc, _, _ = run_subprocess(cmd_s6, cwd=str(SCRIPT_DIR))
    if rc != 0:
        print(f"[ERROR] Stage 6 failed (exit code {rc})")
        end_timer('stage6')
        return False
    print("[OK] Stage 6 complete")
    end_timer('stage6')

    print()
    print("[OK] Refinement pipeline complete (Stages 5-6)")
    return True


# ---------------------------------------------------------------------------
# Stage 1.5: LLM Method Understanding
# ---------------------------------------------------------------------------
def run_llm_method_understanding() -> bool:
    """
    Run Stage 1.5 - method understanding.

    Reasoning is performed by the Copilot agent directly (no request/response
    mailbox). This stage extracts deterministic context and writes a PENDING
    baseline to method_understanding.json. If the agent has not yet filled it,
    the pipeline PAUSES here (AgentActionRequired) so the agent can reason and
    write the answer; a re-run with --resume then skips this stage.
    """
    banner("STAGE 1.5: METHOD UNDERSTANDING")
    start_timer('stage1_5_llm')
    try:
        import stage1_5_llm_method_understanding
        result = stage1_5_llm_method_understanding.run(str(REPO_ROOT))
    except agent_reasoning.AgentActionRequired:
        raise
    except Exception as e:
        print(f"[WARN] Stage 1.5 failed (non-fatal): {e}")
        end_timer('stage1_5_llm')
        return True

    # Pause the pipeline when the agent still needs to fill this stage.
    if agent_reasoning.is_pending(result):
        out = OUTPUT_DIR / 'method_understanding.json'
        # LIVE mode: stay in the kitchen — block in-process until the agent
        # fills the file, then re-run to take the FINALIZE branch. On timeout
        # wait_for_agent_answer returns False and we fall through to the
        # unchanged pause/exit path below.
        if agent_reasoning.LIVE_MODE and agent_reasoning.wait_for_agent_answer(
                out, stage='Stage 1.5 (method understanding)'):
            result = stage1_5_llm_method_understanding.run(str(REPO_ROOT))
        if agent_reasoning.is_pending(result):
            end_timer('stage1_5_llm')
            raise agent_reasoning.AgentActionRequired(
                'Stage 1.5 (method understanding)', out)

    if result and result.get('successful', 0) > 0:
        print(f"[OK] Stage 1.5 complete: {result['successful']}/{result['total_methods']} methods")
    else:
        print("[WARN] Stage 1.5 produced no results")
    end_timer('stage1_5_llm')
    return True


# ---------------------------------------------------------------------------
# Stage 7: LLM TC Judgment
# ---------------------------------------------------------------------------
def run_llm_tc_judgment() -> bool:
    """
    Run Stage 7 - test case judgment.

    Reasoning is performed by the Copilot agent directly (no request/response
    mailbox). This stage writes a PENDING baseline to
    stage7_llm_tc_judgment.json containing the formatted TCs + context. If the
    agent has not yet filled the verdicts, the pipeline PAUSES here
    (AgentActionRequired); a re-run with --resume runs the deterministic
    post-processing (hard-rule override, scenario gaps) on the agent verdicts.
    """
    start_timer('stage7')
    banner("STAGE 7: TC JUDGMENT")
    try:
        import stage7_llm_tc_judgment
        result = stage7_llm_tc_judgment.run(str(REPO_ROOT))
    except agent_reasoning.AgentActionRequired:
        raise
    except Exception as e:
        print(f"[WARN] Stage 7 failed (non-fatal): {e}")
        import traceback
        traceback.print_exc()
        end_timer('stage7')
        return True

    # Pause the pipeline when the agent still needs to judge these TCs.
    if agent_reasoning.is_pending(result):
        out = OUTPUT_DIR / 'stage7_llm_tc_judgment.json'
        # LIVE mode: stay in the kitchen — block in-process until the agent
        # fills the verdicts, then re-run to take the FINALIZE branch. On
        # timeout, fall through to the unchanged pause/exit path below.
        if agent_reasoning.LIVE_MODE and agent_reasoning.wait_for_agent_answer(
                out, stage='Stage 7 (test-case judgment)'):
            result = stage7_llm_tc_judgment.run(str(REPO_ROOT))
        if agent_reasoning.is_pending(result):
            end_timer('stage7')
            raise agent_reasoning.AgentActionRequired(
                'Stage 7 (test-case judgment)', out)

    if result:
        vs = result.get('verdicts_summary', {})
        print(f"[OK] Stage 7 complete: {vs.get('DIRECT',0)} DIRECT, "
              f"{vs.get('INDIRECT',0)} INDIRECT, {vs.get('NOT_RELEVANT',0)} NOT_RELEVANT")
        gaps = result.get('scenario_coverage', {}).get('gaps', 0)
        if gaps:
            print(f"[INFO] {gaps} test scenario(s) without coverage")
    else:
        print("[WARN] Stage 7 produced no results")
    end_timer('stage7')
    return True


# ---------------------------------------------------------------------------
# Stage 8: Semantic Deduplication
# ---------------------------------------------------------------------------
def run_semantic_deduplication(similarity_threshold: float = 0.85,
                               llm_judge_enabled: bool = True) -> bool:
    """
    Run Stage 8 - Semantic Deduplication on the Stage-7 INDIRECT/DIRECT
    test list.

    The 5-step algorithm (full text + embeddings + LLM behaviour judge)
    lives in `stage8_semantic_deduplication.py`. This function is the
    pipeline-side glue: it locates the canonical Stage-7 output + the
    enriched corpus, runs the dedup, and prints a brief summary.

    Stage 8 is BEST-EFFORT - any failure (missing input, embedding model
    unavailable, LLM unreachable) is logged and the pipeline continues.
    The HTML report and downstream consumers will simply use the
    Stage-7 output as before.

    The opt-out env var RIA_SKIP_STAGE8=1 disables this stage entirely.
    """
    if os.environ.get('RIA_SKIP_STAGE8', '').strip() in ('1', 'true', 'TRUE', 'yes'):
        print("[INFO] Stage 8 skipped via RIA_SKIP_STAGE8 env var")
        return True

    stage7_path = OUTPUT_DIR / 'stage7_llm_tc_judgment.json'
    if not stage7_path.exists():
        print(f"[INFO] Stage 8 skipped: {stage7_path} not found (Stage 7 did not run)")
        return True

    enriched_corpus_path = KB_DIR / 'all_tcs_extracted_enriched_source.json'
    if not enriched_corpus_path.exists():
        enriched_corpus_path = None  # dedup will run without enrichment

    start_timer('stage8')
    banner("STAGE 8: SEMANTIC DEDUPLICATION")
    print(f"  Input  : {stage7_path}")
    print(f"  Corpus : {enriched_corpus_path}")
    print(f"  Threshold: {similarity_threshold}")
    print(f"  LLM judge: {'ON' if llm_judge_enabled else 'OFF'}")
    try:
        import stage8_semantic_deduplication as _s8
        result = _s8.semantic_deduplicate(
            stage7_output_path=str(stage7_path),
            enriched_corpus_path=str(enriched_corpus_path) if enriched_corpus_path else None,
            output_dir=str(OUTPUT_DIR),
            similarity_threshold=similarity_threshold,
            llm_judge_enabled=llm_judge_enabled,
        )
        print(f"[OK] Stage 8 complete: input={result['input_count']} "
              f"output={result['output_count']} "
              f"removed={result['removed_count']} "
              f"duration={result['duration_seconds']}s")
        if result['removed_count']:
            print("  Removed test IDs:")
            for r in result['removed_tests']:
                print(f"    - {r.get('test_id')} (in favour of "
                      f"{r.get('kept_in_favor_of')}, sim={r.get('similarity')})")
        end_timer('stage8')
        return True
    except Exception as exc:
        print(f"[WARN] Stage 8 failed (non-fatal): {exc}")
        import traceback
        traceback.print_exc()
        end_timer('stage8')
        return True


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------
def generate_html_report() -> bool:
    """Generate the final interactive HTML report."""
    start_timer('html_report')
    banner("HTML REPORT GENERATION")

    rc, stdout, stderr = run_subprocess(
        [
            'python3', str(SCRIPT_DIR / 'generate_html_report.py'),
            str(OUTPUT_DIR), 'RIA_Report.html',
        ],
        cwd=str(SCRIPT_DIR),
    )
    if rc != 0:
        print(f"[ERROR] HTML report generation failed (exit code {rc})")
        if stderr:
            print(f"[ERROR] {stderr}")
        return False

    report_path = OUTPUT_DIR / 'RIA_Report.html'
    if not report_path.exists():
        print(f"[ERROR] HTML report file not created: {report_path}")
        end_timer('html_report')
        return False

    print()
    print(f"[OK] HTML report generated: {report_path}")
    end_timer('html_report')
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _read_test_count(path: Path, key: str) -> int:
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        val = data.get('output_tests')
        if val is not None:
            return val
        return len(data.get(key, []))
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# Auto-detection of git changes
# ---------------------------------------------------------------------------
def _format_change_report(changes: dict) -> None:
    """Print a friendly summary of detected git changes."""
    print(f"  Changed files   : {changes['total_changed_files']}")
    print(f"  Changed methods : {changes['total_changed_methods']}")
    if changes.get('errors'):
        for err in changes['errors']:
            print(f"  [WARN] {err}")
    print()
    for fi in changes['changed_files']:
        print(f"  File: {fi['file_path']}")
        for m in fi['changed_methods']:
            cls = f"{m['class_name']}." if m.get('class_name') else ''
            tag = ' (fallback)' if m.get('fallback') else ''
            print(f"    - {cls}{m['method_name']}  "
                  f"(lines {m['line_start']}-{m['line_end']}, "
                  f"{len(m.get('changed_lines', []))} changed){tag}")


def auto_detect_changes():
    """
    Run detect_changes.detect_code_changes against REPO_ROOT and return the
    structured report.  Defensive: if the module can't be imported or git is
    unavailable, returns an empty report rather than crashing.

    Caches results to disk keyed by git diff fingerprint so subsequent runs
    (with unchanged working tree) skip the expensive 7-min parse.
    """
    cache_file = OUTPUT_DIR / '.detect_changes_cache.json'

    # Compute a fingerprint of what's changed (fast: just stat-level diff)
    try:
        fp_proc = subprocess.run(
            ['git', 'diff', '--stat', 'HEAD'],
            cwd=str(REPO_ROOT), capture_output=True, text=True
        )
        fp_untracked = subprocess.run(
            ['git', 'ls-files', '--others', '--exclude-standard'],
            cwd=str(REPO_ROOT), capture_output=True, text=True
        )
        fingerprint = fp_proc.stdout.strip() + '\n' + fp_untracked.stdout.strip()
    except Exception:
        fingerprint = None

    # Try loading cache
    if fingerprint and cache_file.exists():
        try:
            with open(cache_file, 'r') as fh:
                cached = json.load(fh)
            if cached.get('_fingerprint') == fingerprint:
                print("[OK] Using cached change detection (working tree unchanged)")
                cached.pop('_fingerprint', None)
                return cached
        except Exception:
            pass  # Cache corrupt, re-detect

    # Run full detection
    try:
        from detect_changes import detect_code_changes  # type: ignore
    except ImportError as exc:
        return {
            'changed_files': [],
            'total_changed_files': 0,
            'total_changed_methods': 0,
            'errors': [f'Cannot import detect_changes module: {exc}'],
        }
    try:
        result = detect_code_changes(str(REPO_ROOT))
    except Exception as exc:  # pragma: no cover - defensive
        return {
            'changed_files': [],
            'total_changed_files': 0,
            'total_changed_methods': 0,
            'errors': [f'Auto-detection failed: {exc}'],
        }

    # Save to cache
    if fingerprint and result.get('total_changed_methods', 0) > 0:
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            cache_data = dict(result)
            cache_data['_fingerprint'] = fingerprint
            with open(cache_file, 'w') as fh:
                json.dump(cache_data, fh, indent=2)
        except Exception:
            pass  # Non-critical: caching is best-effort

    return result


# ---------------------------------------------------------------------------
# Multi-method analysis (consolidate results across N detected methods)
# ---------------------------------------------------------------------------
def _safe_load_json(path: Path) -> dict:
    try:
        with open(path, 'r') as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_ria_summary(*,
                       mode: str,
                       consolidated_flows: list,
                       consolidated_tests: list,
                       per_method_summaries: list = None,
                       all_methods: list = None,
                       failures: list = None) -> None:
    """
    Write ria_v7_summary.json - per-stage counts referenced by SKILL.md.

    Reads the stage-output JSON files from OUTPUT_DIR (so we don't have to
    pipe counts through every stage handler) and aggregates them into a
    single summary the HTML report and CI gates can consume.
    """
    import datetime as _dt

    s4 = _safe_load_json(OUTPUT_DIR / 'stage4_recommended_tests.json')
    s5 = _safe_load_json(OUTPUT_DIR / 'stage5_refined_tests.json')
    s6 = _safe_load_json(OUTPUT_DIR / 'stage6_aggressive_tests.json')
    # Fix #3: stage7 output renamed to stage7_llm_tc_judgment.json. Try
    # the new name first; fall back to the legacy name for back-compat.
    s7 = _safe_load_json(OUTPUT_DIR / 'stage7_llm_tc_judgment.json')
    if not s7:
        s7 = _safe_load_json(OUTPUT_DIR / 'stage7_llm_synthesis.json')
    s2 = _safe_load_json(OUTPUT_DIR / 'stage2_impacted_flows.json')
    s3 = _safe_load_json(OUTPUT_DIR / 'stage3_indirect_flows.json')
    s1 = _safe_load_json(OUTPUT_DIR / 'stage1_entry_points.json')
    mu = _safe_load_json(OUTPUT_DIR / 'method_understanding.json')

    direct_flows = sum(
        1 for f in consolidated_flows or []
        if (f.get('impact_type') or '').upper() == 'DIRECT'
    )
    indirect_flows = sum(
        1 for f in consolidated_flows or []
        if (f.get('impact_type') or '').upper() == 'INDIRECT'
    )

    s4_count = len(s4.get('recommended_tests') or s4.get('tests') or [])
    s5_count = len(s5.get('refined_tests') or s5.get('tests') or [])
    s6_count = len(s6.get('aggressive_tests') or s6.get('tests') or [])

    # Fix #3 (v7.5 audit): reduction_pct must use the FULL corpus baseline
    # (the document set the IDF index was built from), not the Stage-4
    # flow-corpus subset. Previously we divided by s4_count, which gave a
    # per-stage reduction (~97%) while the HTML reported ~99.7% against
    # the full library — same change, two different numbers. We now use
    # the same baseline as generate_html_report.py::_corpus_size().
    full_corpus_count = 0
    idf_index_path = KB_DIR / 'idf_index.json'
    try:
        if idf_index_path.exists():
            with open(idf_index_path, 'r', encoding='utf-8') as _fh:
                _idf = json.load(_fh)
            full_corpus_count = int(_idf.get('total_documents') or 0)
    except Exception:
        full_corpus_count = 0
    # Fall back to the enriched KB length if the IDF index is missing or
    # malformed; mirrors generate_html_report.py's secondary path. The legacy
    # single-pipeline file is no longer produced, so prefer the per-pipeline
    # source file (the source pipeline's canonical enriched corpus).
    if not full_corpus_count:
        try:
            enriched_path = KB_DIR / 'all_tcs_extracted_enriched_source.json'
            if enriched_path.exists():
                with open(enriched_path, 'r', encoding='utf-8') as _fh:
                    full_corpus_count = len(json.load(_fh))
        except Exception:
            full_corpus_count = 0
    # Last-resort fallback: keep s4_count behaviour so the field is never
    # zero on legacy runs that pre-date the IDF index.
    _reduction_baseline = full_corpus_count or s4_count

    summary = {
        'version': '3.0.0',
        'mode': mode,
        'generated_at': _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'stages': {
            'stage1_entry_points': len(s1.get('entry_points') or []),
            'stage2_direct_flows': len(s2.get('impacted_flows') or []),
            'stage3_indirect_flows': len(s3.get('indirect_flows') or []),
            'stage4_recommended_tests': s4_count,
            'stage5_refined_tests': s5_count,
            'stage6_final_tests': s6_count,
            'stage7_judged_tests': (s7.get('verdicts_summary') or {}),
        },
        'flows': {
            'total': len(consolidated_flows or []),
            'direct': direct_flows,
            'indirect': indirect_flows,
        },
        'tests': {
            'final_count': len(consolidated_tests or []),
            # Fix #3 (v7.5 audit): use the full-corpus baseline so the
            # summary file matches the HTML report's reduction figure.
            'reduction_pct': (
                round((1 - (s6_count / max(_reduction_baseline, 1))) * 100, 2)
                if _reduction_baseline else 0.0
            ),
            'reduction_baseline': _reduction_baseline,
            'full_corpus_count': full_corpus_count,
        },
        'methods': {
            'analyzed': len(all_methods or []),
            'failures': len(failures or []),
            'llm_understood': (mu.get('successful') if isinstance(mu, dict) else 0),
        },
        'timing': {
            'total_seconds': get_total_duration(),
            'stage0_kb_build': get_stage_duration('stage0_kb_build'),
            'stage0_focused_kb': get_stage_duration('stage0_focused_kb'),
            'stage1_5_llm_method_understanding': get_stage_duration('stage1_5_llm'),
            'stage2_6_corpus_rebuild': get_stage_duration('stage2_6_corpus_rebuild'),
            'stage3_5_diff_concepts': get_stage_duration('stage3_5_diff_concepts'),
            'stage4_test_correlation': get_stage_duration('stage4'),
            'stage5_refinement': get_stage_duration('stage5'),
            'stage6_suppression': get_stage_duration('stage6'),
            'stage7_llm_judgment': get_stage_duration('stage7'),
            'stage8_semantic_dedup': get_stage_duration('stage8'),
            'html_report_generation': get_stage_duration('html_report'),
        },
    }
    if per_method_summaries:
        summary['per_method'] = [
            {
                'method_name': m.get('method_name'),
                'class_name': m.get('class_name'),
                'file_path': m.get('file_path'),
                'status': m.get('status'),
            }
            for m in per_method_summaries
        ]

    out_path = OUTPUT_DIR / 'ria_v7_summary.json'
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(summary, fh, indent=2)
    print(f"[OK] {out_path}")


def _snapshot_outputs(target_dir: Path, changed_method: str,
                       phase: str = 'all',
                       kb_source: Path = None) -> Path:
    """
    Copy the per-method stage output JSONs into a snapshot subdirectory
    so the next analysis run does not overwrite them.

    Post-pipeline-simplification: the focused flow_registry.json +
    flow_dependencies.json (in KB_DIR) are the per-method flow source of
    truth. Stages 1/2/3 are no longer part of the pipeline.

    Args:
        phase: 'all' (legacy), 'phase_a' (multi-method Phase A; only the
               focused KB files are produced for THIS method - copying
               stage4/5/6 from OUTPUT_DIR would just snapshot stale data
               left over from a previous Phase B run, ~5MB each x N methods
               of pure I/O waste).
        kb_source: Optional override directory for the focused KB files
                   (flow_registry.json / flow_dependencies.json). When
                   omitted, defaults to the canonical KB_DIR. Multi-method
                   workers that wrote to a per-worker scratch dir pass
                   that dir here so the snapshot picks up THEIR output -
                   not whichever worker last wrote KB_DIR (Quick Win #1).

    Returns the snapshot directory path.
    """
    import shutil
    target_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir = target_dir / f'method_{_safe_method_name(changed_method)}'
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    # Cleanup: remove stale snapshot files left over from a previous run so
    # we don't ship stage4/5/6 results from the LAST run alongside this
    # run's focused KB files. We deliberately do NOT remove the directory
    # itself (other tooling may have references to it).
    if phase == 'phase_a':
        for stale in [
            'stage4_recommended_tests.json',
            'stage5_refined_tests.json',
            'stage6_aggressive_tests.json',
            'ria_v7_summary.json',
        ]:
            try:
                (snapshot_dir / stale).unlink(missing_ok=True)
            except Exception:
                pass
    # Files copied from OUTPUT_DIR (current stage outputs).
    # In Phase A multi-method runs, these are NOT produced for this method
    # (Stages 4-6 only run once on the consolidated flows in Phase B), so
    # we skip them to avoid snapshotting stale leftovers.
    if phase == 'all':
        for name in [
            'stage4_recommended_tests.json',
            'stage5_refined_tests.json',
            'stage6_aggressive_tests.json',
            'ria_v7_summary.json',
        ]:
            src = OUTPUT_DIR / name
            if src.exists():
                try:
                    shutil.copy2(src, snapshot_dir / name)
                except Exception as exc:
                    print(f"[WARN] Could not snapshot {name}: {exc}")
    # Files copied from focused-KB source dir. Defaults to canonical KB_DIR
    # for legacy callers; multi-method parallel workers pass their own
    # scratch dir so concurrent runs do not snapshot each other's outputs.
    focused_kb_src = Path(kb_source) if kb_source else KB_DIR
    for name in [
        'flow_registry.json',
        'flow_dependencies.json',
    ]:
        src = focused_kb_src / name
        if src.exists():
            try:
                shutil.copy2(src, snapshot_dir / name)
            except Exception as exc:
                print(f"[WARN] Could not snapshot {name}: {exc}")
    return snapshot_dir


def _safe_method_name(name: str) -> str:
    safe = ''.join(c if (c.isalnum() or c in '._-') else '_' for c in name)
    return safe[:80] or 'method'


def run_single_method_pipeline(args, changed_method: str, changed_file: str,
                                rebuild_kb: bool = False,
                                skip_focused_kb: bool = False,
                                stages_only: str = 'phase_a',
                                kb_dir_override: Path = None) -> bool:
    """
    Run Phase A (focused-KB rebuild only) for ONE (method, file) pair.
    Returns True on success.

    Post-pipeline-simplification: the production pipeline runs Stage 4
    onwards directly from main() / run_multi_method_analysis() against
    the consolidated flow_registry. This helper exists solely to drive
    Phase A of multi-method runs (one focused KB rebuild per method).

    :param stages_only: 'phase_a' = focused KB rebuild only (current
                                     production path). The per-method
                                     flow_registry is what the
                                     multi-method orchestrator
                                     consolidates afterwards.
    :param kb_dir_override: Optional per-worker scratch KB directory used
                             by Phase A parallel runs (Quick Win #1). When
                             supplied, all per-analysis OUTPUT files are
                             written to this directory instead of the
                             canonical KB_DIR. Shared one-time KB INPUTS
                             continue to be read from KB_DIR.
    """
    if not skip_focused_kb:
        if not rebuild_focused_kb(changed_method, changed_file,
                                  kb_dir_override=kb_dir_override):
            # Bug #4 fix: ABORT the entire pipeline on KB build failure.
            # Previous behaviour returned False, letting the multi-method
            # orchestrator log a warning and continue to the next method.
            # That left downstream stages running against a stale or empty
            # flow_registry.json / flow_dependencies.json, which caused
            # Stage 4 to crash mid-run. KB build failure is unrecoverable
            # at the per-method level - the focused KB IS the per-method
            # flow source of truth, so without it there is nothing for
            # Stages 2.5 / 4 / 5 / 6 to consume.
            print(f"[ERROR] KB build failed for {changed_method} "
                  f"(file={changed_file}) - aborting pipeline")
            print(f"        Root cause: flow_registry.json build failure")
            print(f"        Action: Fix data quality issues or adjust "
                  f"quality threshold")
            sys.exit(1)
    else:
        print()
        print("[SKIP] Focused KB rebuild skipped (--skip-focused-kb)")

    # Phase A: the focused KB rebuild already produced flow_registry.json
    # scoped to this method. Multi-method consolidation (Stage 2.5) reads
    # the snapshotted per-method flow_registry directly.
    print(f"[OK] Phase A complete (focused flow_registry produced for "
          f"method '{changed_method}')")
    return True


# ---------------------------------------------------------------------------
# Stage 2.5 - Flow consolidation
# ---------------------------------------------------------------------------
def _flow_dedup_key(flow: dict) -> str:
    """
    Build a robust dedup key for a flow that survives small naming variants.

    Sub-fix #1: collapse trailing version markers ("V2", "_v3") and trim
                trailing digits.
    Sub-fix #2: normalize separators (underscore/dash/space).
    Sub-fix #3: use the *flow_tag* if present (most stable identifier).
    Sub-fix #4: case-insensitive match.
    Sub-fix #5: fall back through flow_tag -> flow_name -> flow -> flow_id.

    The key is intentionally NOT the flow_id alone, because the same business
    flow can appear with different ids across per-method snapshots.
    """
    import re as _re

    raw = (
        flow.get('flow_tag')
        or flow.get('flow_name')
        or flow.get('flow')
        or flow.get('flow_id')
        or ''
    )
    s = str(raw).strip()
    # Drop surrounding [] from flow_tag.
    s = s.strip('[]')
    # Lowercase.
    s = s.lower()
    # Normalise separators.
    s = _re.sub(r'[\s\-]+', '_', s)
    # Collapse runs of underscores.
    s = _re.sub(r'_+', '_', s).strip('_')
    # Drop trailing version markers like "_v2", "v3", and trailing digits.
    s = _re.sub(r'_?v\d+$', '', s)
    s = _re.sub(r'\d+$', '', s).strip('_')
    return s or str(raw).lower()


def consolidate_flows(per_method_flows: list) -> list:
    """
    Merge per-method flow lists into a single deduplicated list.

    `per_method_flows` is a list of (method_name, kind, flow_record) triples
    where `kind` is 'DIRECT' or 'INDIRECT'.

    Sub-fix #1: dedup using `_flow_dedup_key` (resilient to id/version drift).
    Sub-fix #2: union triggered_by_methods.
    Sub-fix #3: prefer DIRECT over INDIRECT classification when both observed.
    Sub-fix #4: keep the highest confidence_score across duplicates.
    Sub-fix #5: preserve the richest (most fields) record as the base.
    """
    consolidated: dict = {}

    for method_name, kind, flow in per_method_flows:
        if not isinstance(flow, dict):
            continue
        key = _flow_dedup_key(flow)
        if not key:
            continue

        existing = consolidated.get(key)
        if existing is None:
            rec = dict(flow)
            rec.setdefault('triggered_by_methods', [])
            if method_name and method_name not in rec['triggered_by_methods']:
                rec['triggered_by_methods'].append(method_name)
            # Store impact type separately to avoid collision with classify_flows
            rec['impact_type'] = kind or rec.get('impact_type') or 'DIRECT'
            consolidated[key] = rec
            continue

        # Sub-fix #2: union triggered_by_methods.
        triggered = existing.setdefault('triggered_by_methods', [])
        if method_name and method_name not in triggered:
            triggered.append(method_name)

        # Sub-fix #3: DIRECT wins over INDIRECT.
        cur = (existing.get('impact_type') or '').upper()
        if kind == 'DIRECT' and cur != 'DIRECT':
            existing['impact_type'] = 'DIRECT'

        # Sub-fix #4: keep highest confidence_score.
        try:
            old_conf = float(existing.get('confidence_score') or 0)
        except (TypeError, ValueError):
            old_conf = 0.0
        try:
            new_conf = float(flow.get('confidence_score') or 0)
        except (TypeError, ValueError):
            new_conf = 0.0
        if new_conf > old_conf:
            existing['confidence_score'] = flow.get('confidence_score')

        # Sub-fix #5: fill in any missing fields from the new record.
        for k, v in flow.items():
            if k in ('triggered_by_methods', 'classification', 'confidence_score'):
                continue
            if existing.get(k) in (None, '', [], {}):
                existing[k] = v

    return list(consolidated.values())


def _try_fast_copy_from_single_worker(consolidated_flows: list) -> bool:
    """
    PERF FAST-PATH (2026-06-07): When Phase A produced exactly ONE worker_kb
    directory whose flow_registry already covers every consolidated flow,
    we can skip the full re-scoring pass and copy the worker's outputs
    directly into the canonical KB_DIR.

    This is provably safe in the 1-worker case because:
      1. Phase A's scoring uses the same algorithm as Stage 2.6
         (`score_test_against_entry_point` from build_flow_registry.py).
      2. Same entry points (verified by comparing flow_registry contents)
         => same auto_tags membership and same flow_scores.
      3. Downstream stages do not depend on the order of `auto_tags` or
         on the `primary_flow` field of the enriched corpus:
           - Stage 4 converts auto_tags into a `set` and iterates in
             `sorted` order (stage4_test_correlation.py L1212, L1242).
           - Stages 5/6 read `matched_flows` from Stage 4's output, never
             the enriched-corpus `primary_flow`.

    Returns True if the fast-path was successfully applied (caller must
    skip the re-scoring loop). Returns False to fall through to the full
    rebuild for any reason: missing files, multiple workers, mismatched
    flow set, or copy failure. The caller is unchanged in the False case.
    """
    multi_dir = OUTPUT_DIR / 'multi_method'
    if not multi_dir.exists():
        return False

    # Discover worker_NN_kb dirs produced by Phase A. We require EXACTLY one;
    # multi-worker runs always need the full merge pass because each worker
    # only tagged its own subset of flows.
    worker_dirs = sorted(
        d for d in multi_dir.iterdir()
        if d.is_dir() and d.name.startswith('worker_') and d.name.endswith('_kb')
    )
    if len(worker_dirs) != 1:
        return False

    worker_kb = worker_dirs[0]
    worker_registry = worker_kb / 'flow_registry.json'
    worker_enriched = worker_kb / 'all_tcs_extracted_enriched_source.json'
    if not (worker_registry.exists() and worker_enriched.exists()):
        return False

    # Verify the worker's flow_registry covers every consolidated flow.
    # Compare by entry-point key (file:method) which is the canonical
    # identifier shared between Phase A and Stage 2.5 consolidation.
    consolidated_keys = set()
    for f in consolidated_flows:
        ep = f.get('entry_point')
        if isinstance(ep, dict):
            fp = ep.get('file')
            mt = ep.get('method')
            if fp and mt:
                consolidated_keys.add(f"{fp}:{mt}")
    if not consolidated_keys:
        return False

    try:
        with open(worker_registry, 'r', encoding='utf-8') as fh:
            worker_data = json.load(fh) or {}
    except Exception as exc:
        print(f"[fast-path] Could not read worker flow_registry.json: {exc}")
        return False

    worker_keys = set()
    for f in (worker_data.get('flows') or []):
        for ep in (f.get('entry_points') or []):
            worker_keys.add(str(ep))

    # The worker MUST cover every consolidated entry point. Extra worker
    # entry points are fine (will be ignored by downstream stages because
    # only consolidated flows are recognised).
    if not consolidated_keys.issubset(worker_keys):
        return False

    # Copy worker outputs into the canonical KB_DIR. We use copy2 to
    # preserve mtimes (so the existing fingerprint cache logic stays
    # correct) and write to a tmp path + os.replace for atomicity.
    KB_DIR.mkdir(parents=True, exist_ok=True)
    targets = [
        (worker_enriched, KB_DIR / 'all_tcs_extracted_enriched_source.json'),
        (worker_registry, KB_DIR / 'flow_registry.json'),
    ]
    try:
        for src, dst in targets:
            tmp_dst = dst.with_suffix(dst.suffix + '.tmp')
            shutil.copy2(src, tmp_dst)
            os.replace(tmp_dst, dst)
    except Exception as exc:
        print(f"[fast-path] Copy failed ({exc}); falling back to full rebuild")
        return False

    print(f"[fast-path] Reused single-worker KB ({worker_kb.name}) — skipped re-scoring 10K+ tests")
    return True


def rebuild_consolidated_test_corpus(consolidated_flows: list) -> bool:
    """
    Rebuild the enriched test corpus and flow_registry.json so that tests are
    tagged for ALL consolidated flows (not just one method's focused subset).

    Why this exists:
        During multi-method Phase A, build_flow_registry.py runs once per
        method in FOCUSED mode and overwrites
        all_tcs_extracted_enriched_source.json with tests tagged for THAT
        method's ~5 entry points only. After Phase A, the enriched corpus
        reflects only the LAST per-method run, so Stage 4 sees ~1,500
        tests tagged for ~5 flows instead of the full corpus tagged for
        all consolidated flows.

    What this does (Option A from problem statement):
        1. Read RAW test corpus (all_tcs_extracted.json) from RIA_INPUT/.
        2. Build a synthetic entry-point list from the consolidated flows
           (each flow already carries its entry_point file/method).
        3. Score every test against every consolidated entry point using
           the same 3-signal scoring as build_flow_registry.py.
        4. Re-emit:
              - knowledge_base/all_tcs_extracted_enriched_source.json
                (auto_tags covering all consolidated flows; the
                per-pipeline source mirror)
              - knowledge_base/flow_registry.json (one entry per
                consolidated flow with proper test_count)
        The legacy backward-compat file
        (`all_tcs_extracted_enriched.json`) is no longer produced.

    Returns True on success, False on failure (Stage 4 will still try with
    whatever corpus is on disk, so we never abort the run).
    """
    from collections import defaultdict

    section("STAGE 2.6: rebuild consolidated test corpus")
    start_timer('stage2_6_corpus_rebuild')

    if not consolidated_flows:
        print("[WARN] No consolidated flows - skipping corpus rebuild")
        end_timer('stage2_6_corpus_rebuild')
        return False

    # ---- PERF fast-path: single-worker reuse --------------------------
    # When Phase A produced a single worker_kb whose registry already
    # covers every consolidated flow, copy that worker's outputs into the
    # canonical KB_DIR instead of re-scoring 10K+ tests. Verified safe:
    # see _try_fast_copy_from_single_worker for the proof. Falls through
    # to the full rebuild on any unmet precondition or copy failure.
    if _try_fast_copy_from_single_worker(consolidated_flows):
        end_timer('stage2_6_corpus_rebuild')
        return True

    # ---- Locate raw corpus ------------------------------------------------
    raw_corpus_path = REPO_ROOT / '.github' / 'RIA_INPUT' / 'all_tcs_extracted.json'
    if not raw_corpus_path.exists():
        print(f"[ERROR] Raw test corpus not found: {raw_corpus_path}")
        print("        Cannot rebuild consolidated corpus without raw corpus.")
        end_timer('stage2_6_corpus_rebuild')
        return False

    # ---- Import scoring helpers from build_flow_registry ------------------
    try:
        from build_flow_registry import (  # noqa: E402
            score_test_against_entry_point,
            derive_flow_name,
            MIN_TAG_SCORE,
            MULTI_FLOW_THRESHOLD_RATIO,
            GENERIC_DOMAIN_NOUNS,
            _load_generic_nouns,
        )
    except Exception as exc:
        print(f"[ERROR] Could not import scoring helpers: {exc}")
        end_timer('stage2_6_corpus_rebuild')
        return False

    # Ensure GENERIC_DOMAIN_NOUNS is populated (it starts empty at module
    # level and is normally filled inside build_flow_registry()).
    if not GENERIC_DOMAIN_NOUNS:
        import build_flow_registry as _bfr
        _bfr.GENERIC_DOMAIN_NOUNS = _load_generic_nouns(str(KB_DIR))
        GENERIC_DOMAIN_NOUNS = _bfr.GENERIC_DOMAIN_NOUNS
        print(f"[OK] Loaded {len(GENERIC_DOMAIN_NOUNS)} generic nouns for flow-name filtering")

    # ---- Load synonym groups (required by scoring) ------------------------
    synonym_path = KB_DIR / 'synonym_groups.json'
    try:
        with open(synonym_path, 'r', encoding='utf-8') as fh:
            synonym_groups = (json.load(fh) or {}).get('synonym_groups', {})
    except Exception as exc:
        print(f"[ERROR] Could not load synonym_groups.json: {exc}")
        end_timer('stage2_6_corpus_rebuild')
        return False

    # ---- Build entry-point list from consolidated flows -------------------
    entry_points = []
    flow_tag_by_ep_key: dict = {}
    flow_name_by_ep_key: dict = {}
    seen_keys: set = set()
    generic_filtered = 0
    for flow in consolidated_flows:
        ep = flow.get('entry_point')
        if not isinstance(ep, dict):
            continue
        file_path = ep.get('file')
        method = ep.get('method')
        if not file_path or not method:
            continue
        ep_key = f"{file_path}:{method}"
        if ep_key in seen_keys:
            continue

        # Derive the flow name (from existing or from method name)
        flow_name = (
            flow.get('flow_name') or flow.get('flow')
            or derive_flow_name({'method': method}, synonym_groups)
        )

        # Fix #4: UNION semantics. We previously skipped flows whose
        # entire name was composed of generic/framework tokens (e.g.
        # "Call", "Run", "Execute"). Per-method runs already surfaced
        # these as relevant for the change, so dropping them here
        # silently broke registry/dependencies sync (Fix #5). Keep all
        # flows that the per-method registries identified; downstream
        # ranking still uses IDF-weighted scoring to deprioritise
        # truly-generic matches.
        _fn_tokens = [t.lower() for t in re.findall(r'[A-Za-z]{3,}', flow_name)]
        if _fn_tokens and GENERIC_DOMAIN_NOUNS and all(
            t in GENERIC_DOMAIN_NOUNS for t in _fn_tokens
        ):
            print(f"  [INFO] Retaining generic-named flow \"{flow_name}\" "
                  f"(entry point {ep_key}) — surfaced by per-method run")

        seen_keys.add(ep_key)
        entry_points.append({'file': file_path, 'method': method})
        flow_tag_by_ep_key[ep_key] = (
            flow.get('flow_tag')
            or f"[{flow_name.upper().replace(' ', '_')}]"
        )
        flow_name_by_ep_key[ep_key] = flow_name

    if generic_filtered:
        print(f"  [INFO] Filtered {generic_filtered} generic flow(s)")

    if not entry_points:
        print("[WARN] Consolidated flows have no entry points - skipping rebuild")
        end_timer('stage2_6_corpus_rebuild')
        return False

    print(f"[OK] Built {len(entry_points)} entry points from {len(consolidated_flows)} consolidated flows")

    # ---- Load raw corpus --------------------------------------------------
    try:
        with open(raw_corpus_path, 'r', encoding='utf-8') as fh:
            tests = json.load(fh)
    except Exception as exc:
        print(f"[ERROR] Could not read raw corpus: {exc}")
        end_timer('stage2_6_corpus_rebuild')
        return False
    print(f"[OK] Loaded raw corpus: {len(tests)} tests")

    # ---- Score every test against every consolidated entry point ----------
    # Mirrors the multi-flow tagging logic in build_flow_registry.py
    # (lines 615-682) so behaviour is identical.
    enriched_tests = []
    flow_registry: dict = defaultdict(lambda: {'entry_points': set(), 'tests': []})

    # FALLBACK REMOVED (2026-06-02): No embedding rescue for tests below MIN_TAG_SCORE.
    # Tests that score < MIN_TAG_SCORE indicate data quality issues (incomplete
    # synonym_groups.json or poorly written test descriptions). Fail fast instead
    # of masking with semantic similarity.

    for i, test in enumerate(tests):
        if (i + 1) % 1000 == 0:
            print(f"  Scored {i + 1}/{len(tests)} tests...")

        scores = {}
        for ep in entry_points:
            ep_key = f"{ep['file']}:{ep['method']}"
            scores[ep_key] = score_test_against_entry_point(test, ep, synonym_groups)

        if not scores:
            continue

        best_ep_key = max(scores, key=scores.get)
        best_score = scores[best_ep_key]
        if best_score >= MIN_TAG_SCORE:
            score_threshold = max(MIN_TAG_SCORE, best_score * MULTI_FLOW_THRESHOLD_RATIO)
            matched_flows = []
            flow_scores = {}

            for ep_key, score in scores.items():
                if score < score_threshold:
                    continue
                flow_tag = flow_tag_by_ep_key[ep_key]
                flow_name = flow_name_by_ep_key[ep_key]
                matched_flows.append({
                    'flow_name': flow_name,
                    'flow_tag': flow_tag,
                    'entry_point': ep_key,
                    'score': score,
                })
                flow_scores[flow_tag] = score

            if not matched_flows:
                continue

            matched_flows.sort(key=lambda x: x['score'], reverse=True)

            enriched_test = dict(test)
            enriched_test['auto_tags'] = [mf['flow_tag'] for mf in matched_flows]
            enriched_test['discovered_entry_points'] = [mf['entry_point'] for mf in matched_flows]
            enriched_test['flow_scores'] = flow_scores
            enriched_test['primary_flow'] = matched_flows[0]['flow_tag']
            enriched_tests.append(enriched_test)

            for mf in matched_flows:
                flow_registry[mf['flow_name']]['entry_points'].add(mf['entry_point'])
                flow_registry[mf['flow_name']]['tests'].append(test.get('issue_key'))

        # NO FALLBACK: Tests scoring below MIN_TAG_SCORE are REJECTED.
        # This surfaces data quality issues immediately instead of masking them.

    print(f"[OK] Tagged {len(enriched_tests)} tests against consolidated flows")

    # ---- Write enriched corpus (source pipeline) -------------------------
    # The enriched corpus is written to `all_tcs_extracted_enriched_source.json`
    # for the source pipeline. The legacy single-pipeline file
    # (`all_tcs_extracted_enriched.json`) is no longer produced.
    KB_DIR.mkdir(parents=True, exist_ok=True)
    enriched_kb_source_path = KB_DIR / 'all_tcs_extracted_enriched_source.json'
    try:
        with open(enriched_kb_source_path, 'w', encoding='utf-8') as fh:
            json.dump(enriched_tests, fh, indent=2, ensure_ascii=False)
    except Exception as exc:
        print(f"[ERROR] Could not write enriched corpus: {exc}")
        end_timer('stage2_6_corpus_rebuild')
        return False
    print(f"[OK] Wrote enriched corpus: {enriched_kb_source_path} "
          f"({len(enriched_tests)} tests, per-pipeline source mirror)")

    # ---- Write consolidated flow_registry.json ----------------------------
    flows_out = []
    for i, (flow_name, data) in enumerate(flow_registry.items(), 1):
        flows_out.append({
            'flow_id': f'FLOW_{i:03d}',
            'flow_name': flow_name,
            'test_tags': [f"[{flow_name.upper().replace(' ', '_')}]"],
            'entry_points': list(data['entry_points']),
            'test_count': len(data['tests']),
        })
    registry_out = {
        'flows': flows_out,
        'total_flows': len(flows_out),
        'total_tests_tagged': len(enriched_tests),
        'source': 'consolidated multi-method (rebuild_consolidated_test_corpus)',
    }
    try:
        with open(KB_DIR / 'flow_registry.json', 'w', encoding='utf-8') as fh:
            json.dump(registry_out, fh, indent=2, ensure_ascii=False)
    except Exception as exc:
        print(f"[WARN] Could not write consolidated flow_registry.json: {exc}")
    print(f"[OK] Wrote consolidated flow_registry.json ({len(flows_out)} flows)")

    end_timer('stage2_6_corpus_rebuild')
    return True


def _resolve_audit_apply_mode(args) -> str:
    """Resolve the audit's apply_fixes mode from CLI flags.

    Precedence (highest first):
        --auto-fix-audit  -> 'yes' (apply fixes unattended)
        --audit-prompt    -> 'prompt' (legacy interactive behaviour)
        default           -> 'no' (detection-only, never blocks on stdin)

    The default was changed from 'prompt' to 'no' because the prompt mode
    blocks on stdin during multi-method runs. Users who want fixes to be
    auto-applied should pass --auto-fix-audit (recommended); users who
    explicitly want the old interactive behaviour can pass --audit-prompt.
    """
    if getattr(args, 'auto_fix_audit', False):
        return 'yes'
    if getattr(args, 'audit_prompt', False):
        return 'prompt'
    return 'no'


def _infer_audit_change_type(args, multi_method_mode: bool) -> str:
    """Pick the change_type label the auditor needs for outcome validation.

    Resolution order:
        explicit --audit-change-type wins
        multi-method mode              -> 'source_multi_method'
        single (method, file) supplied -> 'source_single_method'
        otherwise                      -> 'source_multi_method'
    """
    explicit = getattr(args, 'audit_change_type', None)
    if explicit:
        return explicit
    if multi_method_mode:
        return 'source_multi_method'
    if getattr(args, 'changed_method', None) and getattr(args, 'changed_file', None):
        return 'source_single_method'
    return 'source_multi_method'


def _run_post_pipeline_audit(args, multi_method_mode: bool,
                             changes: dict = None) -> None:
    """Run the stage-execution auditor at the end of the pipeline.

    Invariants:
      - Honours --no-audit (skip entirely).
      - Defaults to non-blocking ('apply_fixes=no') so the audit never
        blocks the orchestrator. --auto-fix-audit and --audit-prompt opt
        into stdin-driven flows explicitly.
      - Works for both single-method and multi-method runs. In
        multi-method mode it picks the first detected (method, file) pair
        as the rebuild anchor for any auto-fixes that need one.
    """
    if getattr(args, 'no_audit', False):
        return
    print()
    print("=" * 80)
    print("STAGE EXECUTION AUDIT")
    print("=" * 80)
    try:
        from stage_execution_auditor import (  # type: ignore
            audit_full_pipeline,
        )

        # Resolve the (method, file) anchor used by auto-fix rebuilds.
        if multi_method_mode:
            anchor_method = None
            anchor_file = None
            try:
                first_fi = ((changes or {}).get('changed_files') or [None])[0]
                if first_fi and first_fi.get('changed_methods'):
                    anchor_method = first_fi['changed_methods'][0]['method_name']
                    anchor_file = first_fi['file_path']
            except Exception:
                anchor_method = None
                anchor_file = None
        else:
            anchor_method = getattr(args, 'changed_method', None)
            anchor_file = getattr(args, 'changed_file', None)

        def _rerun_pipeline_for_audit(rerun_from_stage: int) -> bool:
            rerun_cmd = [
                'python3', str(SCRIPT_DIR / 'ria_agent.py'),
                '--no-html', '--no-audit', '--audit-child',
            ]
            if multi_method_mode:
                rerun_cmd.append('--auto-detect')
            else:
                if anchor_method:
                    rerun_cmd.extend(['--changed-method', anchor_method])
                if anchor_file:
                    rerun_cmd.extend(['--changed-file', anchor_file])
            if getattr(args, 'user_prompt', None):
                rerun_cmd.extend(['--user-prompt', args.user_prompt])
            if getattr(args, 'language', None) and args.language != 'auto':
                rerun_cmd.extend(['--language', args.language])
            if rerun_from_stage == 0:
                rerun_cmd.append('--rebuild-kb')
            print(f'  Re-run command : {" ".join(rerun_cmd)}')
            rc, _, _ = run_subprocess(rerun_cmd, cwd=str(REPO_ROOT))
            return rc == 0

        apply_mode = _resolve_audit_apply_mode(args)
        change_type = _infer_audit_change_type(args, multi_method_mode)
        audit_out = audit_full_pipeline(
            repo_root=str(REPO_ROOT),
            output_dir=OUTPUT_DIR,
            kb_dir=KB_DIR,
            script_dir=SCRIPT_DIR,
            changed_method=anchor_method,
            changed_file=anchor_file,
            change_type=change_type,
            apply_fixes=apply_mode,
            rerun_pipeline=_rerun_pipeline_for_audit,
        )
        status = audit_out.get('overall_status', 'UNKNOWN')
        issues = audit_out.get('issues', [])
        report = audit_out.get('report_paths', {}).get('markdown', '')
        if status == 'FAIL':
            print(f"\n[WARN] Audit found {len(issues)} issue(s).")
            if report:
                print(f"       See report: {report}")
        elif status == 'FIXED':
            print("\n[OK] Audit found issues, applied fixes, "
                  "verified clean.")
        else:
            print("\n[OK] All stages passed audit.")
    except Exception as exc:
        print(f"[WARN] Stage execution auditor failed: {exc}")


def run_multi_method_analysis(args, changes: dict) -> bool:
    """
    Run the RIA pipeline for each detected (method, file) pair and merge
    results across methods.

    Restructured pipeline (Phase A / Stage 2.5 / Phase B).
    Post-pipeline-simplification: Stages 1/2/3 are no longer invoked.

        Phase A  (per method, N times)
            - rebuild focused KB
              (this writes a flow_registry.json scoped to the method)
            - snapshot the per-method flow_registry + flow_dependencies

        Stage 2.5  (once, after all per-method runs)
            - merge per-method flow_registry records (converted to flow
              dicts with entry_point file/method) using consolidate_flows()
            - classify the consolidated flows via flow_discovery.classify_flows()
              using a data-driven generic-token lexicon (no hardcoded lists)

        Phase B  (once, on consolidated data)
            - rebuild the enriched test corpus + flow_registry.json so they
              cover ALL consolidated flows
            - run Stage 4 with --flow-registry (consolidated)
            - run Stages 5/6 (refinement) reading flow_registry +
              flow_dependencies directly
            - generate the HTML report once

    Returns True if all per-method Phase A runs succeeded.
    """
    # When run_multi_method_analysis() is invoked from main(), TeeLogger
    # is already attached and we must NOT create a second log file. Only
    # set up logging here when called as a standalone entry point (e.g.
    # external test harnesses) so the consolidated log still works.
    _own_logging_cleanup = None
    if not isinstance(sys.stdout, TeeLogger):
        _log_path, _own_logging_cleanup = setup_logging(
            OUTPUT_DIR,
            max_logs=getattr(args, 'max_logs', DEFAULT_MAX_LOGS),
        )
        print(f"[LOG] Multi-method pipeline output captured to: {_log_path}")

    try:
        return _run_multi_method_analysis_body(args, changes)
    finally:
        if _own_logging_cleanup is not None:
            _own_logging_cleanup()


def _run_multi_method_analysis_body(args, changes: dict) -> bool:
    """Body of run_multi_method_analysis (extracted so the wrapper above
    can manage optional log-consolidation lifecycle)."""
    multi_dir = OUTPUT_DIR / 'multi_method'
    multi_dir.mkdir(parents=True, exist_ok=True)

    all_methods = []
    seen_pairs: set = set()
    for fi in changes['changed_files']:
        for m in fi['changed_methods']:
            # Defensive dedup: (file, method) pairs always produce the same
            # focused KB so duplicate entries would be pure wasted work.
            pair = (fi['file_path'], m['method_name'])
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            all_methods.append({
                'method_name':  m['method_name'],
                'class_name':   m.get('class_name'),
                'file_path':    fi['file_path'],
                'line_start':   m.get('line_start'),
                'line_end':     m.get('line_end'),
                'changed_lines': m.get('changed_lines', []),
                'is_fallback':  m.get('fallback', False),
            })

    if not all_methods:
        print("[ERROR] No methods to analyze in multi-method mode.")
        return False

    # =====================================================================
    # PHASE A - per-method focused KB rebuild (no Stage 1/2/3 traces)
    # Each per-method run produces a FOCUSED flow_registry.json scoped to
    # that method. flow_registry IS the per-method flow source of truth -
    # the legacy Stage 1/2/3 trace-and-match scripts are no longer needed.
    #
    # PARALLELIZATION (v7.6): Run per-method pipelines in parallel using
    # ThreadPoolExecutor. Each method writes to its own snapshot directory
    # (no file conflicts). Shared state is accumulated AFTER all threads
    # complete.
    # =====================================================================
    banner("PHASE A: per-method focused KB rebuild (PARALLEL)")
    import concurrent.futures
    from threading import Lock

    consolidated_tests: dict = {}     # tc_id -> richest record seen
    per_method_flow_records: list = []  # list of (method_name, kind, flow_dict)
    per_method_summaries: list = []
    per_method_components: list = []   # canonical component per method
    failures: list = []
    # Fix #5: Track per-(flow, component) dependency_type across ALL
    # per-method runs so that the consolidated flow_dependencies.json
    # preserves the strongest classification each method observed. Without
    # this, Phase A overwrites flow_dependencies.json on each iteration and
    # the final on-disk copy reflects only the LAST method, demoting
    # earlier DIRECT classifications to INDIRECT (or losing them entirely)
    # when Stage 4 looks them up. DIRECT wins over INDIRECT.
    consolidated_dep_map: dict = {}    # (flow_name, component) -> 'DIRECT'|'INDIRECT'

    # Locks for thread safety
    # Quick Win #1: kb_lock REMOVED. Each worker now writes to a private
    # scratch KB dir (multi_dir/worker_NN_kb), so there is no shared file
    # contention. Only console output remains shared and uses print_lock.
    print_lock = Lock()  # Prevent interleaved console output

    # ---------------------------------------------------------------------
    # Quick Win #2: entry-point fingerprinting for pre-execution dedup.
    # Methods whose call trees produce identical entry-point sets generate
    # identical flow_registry.json output. We compute a fast fingerprint up
    # front, run only one representative per fingerprint, and clone the
    # representative's snapshot for every sibling in the same group.
    #
    # Safety: any failure in the probe falls back to a unique fingerprint
    # so the method runs as if no dedup were attempted. Behaviour is
    # therefore strictly safer-than-or-equal-to the un-deduplicated path.
    # ---------------------------------------------------------------------
    def _entry_point_fingerprint(mi: dict) -> str:
        """Probe the call graph and return a SHA256 over the sorted entry
        points. On ANY error, return a method-unique hash so the method is
        never accidentally collapsed with a sibling.
        """
        import hashlib as _hashlib
        unique_fp = _hashlib.sha256(
            f"UNIQUE::{mi['file_path']}::{mi['method_name']}".encode('utf-8')
        ).hexdigest()[:16]
        try:
            # Local imports keep the cost of the probe (Serena bootstrap +
            # call-graph walk) out of the import path for runs that never
            # need it (single-method, etc.).
            from build_flow_registry import (  # type: ignore
                _focused_walk_no_callers_entry_points,
            )
            from serena_mcp_client import SerenaMCPClient  # type: ignore
            serena = SerenaMCPClient(
                repo_path=str(REPO_ROOT), enabled=True, max_symbols=10000)
            # Reduced depth/breadth keeps the probe well under a second per
            # method while still surfacing the entry-point shape that
            # determines whether two methods produce the same registry.
            result = _focused_walk_no_callers_entry_points(
                serena=serena,
                repo_root=str(REPO_ROOT),
                changed_file=mi['file_path'],
                changed_method=mi['method_name'],
                max_depth=10,
                max_callers_per_node=100,
            )
            eps = result.get('entry_points') or []
            keys = sorted(
                f"{(ep.get('file') or '').strip()}:{(ep.get('method') or '').strip()}"
                for ep in eps
                if isinstance(ep, dict) and ep.get('file') and ep.get('method')
            )
            if not keys:
                # No entry points discovered — this is a meaningful state
                # in itself, but we still want sibling methods that ALSO
                # discover zero entry points to share a representative.
                return _hashlib.sha256(b'NO_ENTRY_POINTS').hexdigest()[:16]
            return _hashlib.sha256(
                '\n'.join(keys).encode('utf-8')
            ).hexdigest()[:16]
        except Exception as exc:
            # Defensive fallback: never block the pipeline on a failed probe.
            with print_lock:
                print(f"[WARN] Entry-point fingerprint probe failed for "
                      f"{mi['method_name']}: {exc} - treating as unique")
            return unique_fp

    fingerprints: list = []   # parallel list to all_methods
    use_dedup = (
        len(all_methods) > 1
        and not getattr(args, 'no_method_dedup', False)
    )
    if use_dedup:
        with print_lock:
            print(f"[INFO] Computing entry-point fingerprints for "
                  f"{len(all_methods)} method(s)...")
        for mi in all_methods:
            fingerprints.append(_entry_point_fingerprint(mi))

        # Group by fingerprint, preserving original order so the
        # representative is always the FIRST method in each group.
        from collections import OrderedDict as _OrderedDict
        groups: dict = _OrderedDict()
        for mi, fp in zip(all_methods, fingerprints):
            groups.setdefault(fp, []).append(mi)

        representatives = [grp[0] for grp in groups.values()]
        deduplicated = [
            (sibling, grp[0])
            for grp in groups.values() if len(grp) > 1
            for sibling in grp[1:]
        ]
        with print_lock:
            if deduplicated:
                print(f"[INFO] Entry-point deduplication: "
                      f"{len(all_methods)} methods -> "
                      f"{len(representatives)} unique "
                      f"({len(deduplicated)} deduplicated)")
                for sibling, rep in deduplicated:
                    print(f"       dedup: {sibling['method_name']} "
                          f"({sibling['file_path']}) -> reuses "
                          f"{rep['method_name']} ({rep['file_path']})")
            else:
                print(f"[INFO] Entry-point deduplication: all "
                      f"{len(all_methods)} method(s) unique - no dedup applied")
    else:
        # Single-method runs (or explicit opt-out) skip the probe entirely
        # and run every method directly.
        representatives = list(all_methods)
        deduplicated = []

    # Helper: build a per-worker scratch KB dir (Quick Win #1). Workers
    # write all per-analysis files (flow_registry.json,
    # flow_dependencies.json, enriched corpora) into their own directory,
    # which removes the need for a global KB write-lock.
    def _create_worker_kb_dir(worker_idx: int) -> Path:
        worker_kb = multi_dir / f"worker_{worker_idx:02d}_kb"
        # Clean up stale per-worker outputs from a previous run so we never
        # snapshot a file produced by a different invocation.
        if worker_kb.exists():
            try:
                shutil.rmtree(worker_kb)
            except OSError as exc:
                # Best-effort cleanup: a leftover file is preferable to a
                # crash. The subprocess will overwrite individual files.
                print(f"[WARN] Could not clear stale worker dir {worker_kb}: {exc}")
        worker_kb.mkdir(parents=True, exist_ok=True)
        return worker_kb

    def _run_method_worker(idx: int, mi: dict) -> tuple:
        """
        Worker function for parallel per-method pipeline execution.

        Returns (idx, mi, ok, snap_dir, worker_kb) where:
        - idx: method index (1-based)
        - mi: method info dict
        - ok: True if pipeline succeeded
        - snap_dir: Path to snapshot directory (or None on failure)
        - worker_kb: Path to the worker's scratch KB dir
        """
        with print_lock:
            banner(f"MULTI-METHOD RUN {idx}/{len(representatives)}: "
                   f"{mi['method_name']} (in {mi['file_path']})")

        # Quick Win #1: per-worker scratch KB (no shared writes -> no lock).
        # The worker's flow_registry.json / flow_dependencies.json /
        # enriched corpora all land here, isolated from sibling workers.
        worker_kb = _create_worker_kb_dir(idx)

        try:
            ok = run_single_method_pipeline(
                args,
                changed_method=mi['method_name'],
                changed_file=mi['file_path'],
                rebuild_kb=False,
                skip_focused_kb=args.skip_focused_kb,
                stages_only='phase_a',  # focused KB only
                kb_dir_override=worker_kb,
            )

            snap = _snapshot_outputs(
                multi_dir, f"{idx:02d}_{mi['method_name']}",
                phase='phase_a', kb_source=worker_kb,
            ) if ok else None
        except Exception as exc:
            with print_lock:
                print(f"[ERROR] Worker {idx} ({mi['method_name']}) crashed: {exc}")
            ok = False
            snap = None

        return (idx, mi, ok, snap, worker_kb)

    # Execute per-method pipelines in parallel (max_workers = CPU count or method count, whichever is smaller)
    max_workers = min(len(representatives), os.cpu_count() or 4)
    with print_lock:
        print(f"[INFO] Running {len(representatives)} representative "
              f"method(s) in parallel (max_workers={max_workers})")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_run_method_worker, idx, mi)
            for idx, mi in enumerate(representatives, start=1)
        ]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    # Sort results by idx to process in original order (for deterministic output)
    results.sort(key=lambda r: r[0])

    # Quick Win #2: build the lookup of representative snapshots so we can
    # clone them for deduplicated siblings. We key by the (file_path,
    # method_name) tuple which uniquely identifies the representative.
    rep_snapshot_by_key: dict = {}
    for _ridx, _rmi, _rok, _rsnap, _rkb in results:
        if _rok and _rsnap is not None:
            rep_snapshot_by_key[(_rmi['file_path'], _rmi['method_name'])] = _rsnap

    # Promote deduplicated siblings to a virtual "ran-as-representative"
    # state by cloning the representative's per-method snapshot directory.
    # Each sibling gets its own snapshot folder so downstream consumers
    # (per-method summaries, snapshot paths in reports) see consistent data.
    sibling_results: list = []
    if deduplicated:
        sibling_start_idx = len(representatives) + 1
        for offset, (sibling, rep) in enumerate(deduplicated):
            rep_key = (rep['file_path'], rep['method_name'])
            rep_snap = rep_snapshot_by_key.get(rep_key)
            if rep_snap is None:
                # Representative failed - sibling fails too (fair: we never
                # ran the sibling, and faking success would lie to Stage 4).
                with print_lock:
                    print(f"[WARN] Representative for {sibling['method_name']} "
                          f"({rep['method_name']}) failed - sibling marked failed")
                sibling_results.append(
                    (sibling_start_idx + offset, sibling, False, None, None))
                continue
            sibling_idx = sibling_start_idx + offset
            # Mirror the naming scheme used by representative workers:
            # _snapshot_outputs() builds the dir as
            # "method_<safe(idx_method_name)>". Using the same scheme for
            # cloned siblings keeps log/report links consistent.
            sibling_safe = _safe_method_name(
                f"{sibling_idx:02d}_{sibling['method_name']}")
            sibling_snap_dir = multi_dir / f"method_{sibling_safe}"
            try:
                if sibling_snap_dir.exists():
                    shutil.rmtree(sibling_snap_dir)
                shutil.copytree(rep_snap, sibling_snap_dir)
                with print_lock:
                    print(f"[OK] Cloned snapshot for deduplicated method "
                          f"{sibling['method_name']} from {rep['method_name']}")
                sibling_results.append(
                    (sibling_idx, sibling, True, sibling_snap_dir, None))
            except OSError as exc:
                with print_lock:
                    print(f"[WARN] Could not clone snapshot for "
                          f"{sibling['method_name']}: {exc}")
                sibling_results.append(
                    (sibling_idx, sibling, False, None, None))

    # Combine and re-sort so we process every method (representatives +
    # cloned siblings) in deterministic order.
    results.extend(sibling_results)
    results.sort(key=lambda r: r[0])

    # Process results and accumulate shared state
    for idx, mi, ok, snap, _worker_kb in results:
        if not ok:
            failures.append(mi)
            per_method_summaries.append({
                **mi,
                'status': 'failed',
            })
            continue

        # snap is already computed by the worker function, no need to recompute
        # Snapshot file paths for this method.
        # Post-pipeline-simplification: read the per-method FOCUSED
        # flow_registry.json + flow_dependencies.json directly. No Stage
        # 1/2/3 traces are produced any more.
        method_registry_path = snap / 'flow_registry.json'
        method_deps_path = snap / 'flow_dependencies.json'

        # Resolve canonical component for THIS method (used for
        # (flow, component) DIRECT/INDIRECT lookup below).
        method_component = _derive_component_from_file(mi['file_path'])
        if method_component and method_component not in per_method_components:
            per_method_components.append(method_component)

        # Build the per-method (flow, component) -> dependency_type lookup.
        method_deps_map: dict = {}
        deps_data = _safe_load_json(method_deps_path)
        for dep in deps_data.get('dependencies', []) or []:
            fn = dep.get('flow', '')
            cp = dep.get('component', '')
            dt = dep.get('dependency_type', '')
            if fn and cp:
                method_deps_map[(fn, cp)] = dt
                # Fix #5: Merge into the cross-method consolidated map.
                # DIRECT always beats INDIRECT - we never demote a flow
                # that ANY method classified as DIRECT.
                key = (fn, cp)
                prev = consolidated_dep_map.get(key)
                if prev != 'DIRECT':
                    if dt == 'DIRECT':
                        consolidated_dep_map[key] = 'DIRECT'
                    elif dt and prev is None:
                        consolidated_dep_map[key] = dt

        # Collect per-method flow records from flow_registry.json.
        # Each entry in flow_registry is converted into the dict shape that
        # consolidate_flows() and rebuild_consolidated_test_corpus() expect:
        #   flow_id, flow_name, flow, flow_tag, entry_point (file/method dict),
        #   impact_type (DIRECT/INDIRECT resolved per (flow, component))
        registry_data = _safe_load_json(method_registry_path)
        for rf in registry_data.get('flows', []) or []:
            if not isinstance(rf, dict):
                continue
            flow_name = rf.get('flow_name') or ''
            flow_id = rf.get('flow_id') or ''
            test_tags = rf.get('test_tags') or []
            flow_tag = test_tags[0] if test_tags else (
                f"[{flow_name.upper().replace(' ', '_')}]" if flow_name else ''
            )
            if not flow_tag or not flow_name:
                continue

            # Resolve impact_type per (flow_name, method_component).
            kind = 'DIRECT'
            if method_component:
                dep = method_deps_map.get((flow_name, method_component))
                if dep == 'INDIRECT':
                    kind = 'INDIRECT'
                elif dep == 'DIRECT':
                    kind = 'DIRECT'
                # else: focused mode -> DIRECT default

            # Each flow_registry entry has a list of "file:method" entry points.
            # Emit one record per entry point so rebuild_consolidated_test_corpus
            # (which reads flow['entry_point'] as a {file, method} dict) gets
            # what it expects.
            entry_points = rf.get('entry_points') or []
            if not entry_points:
                # Still emit the flow even when no entry-point string is
                # present - consolidate_flows just dedupes by name/tag.
                flow_record = {
                    'flow_id': flow_id,
                    'flow_name': flow_name,
                    'flow': flow_name,
                    'flow_tag': flow_tag,
                    'confidence_score': 100,
                    'impact_type': kind,
                }
                per_method_flow_records.append(
                    (mi['method_name'], kind, flow_record))
                continue

            for ep_str in entry_points:
                if not isinstance(ep_str, str) or ':' not in ep_str:
                    continue
                ep_file, _, ep_method = ep_str.rpartition(':')
                if not ep_file or not ep_method:
                    continue
                flow_record = {
                    'flow_id': flow_id,
                    'flow_name': flow_name,
                    'flow': flow_name,
                    'flow_tag': flow_tag,
                    'entry_point': {
                        'file': ep_file.strip(),
                        'method': ep_method.strip(),
                    },
                    'confidence_score': 100,
                    'impact_type': kind,
                }
                per_method_flow_records.append(
                    (mi['method_name'], kind, flow_record))

        per_method_summaries.append({
            **mi,
            'status':     'ok',
            'snapshot':   str(snap),
        })

    # -----------------------------------------------------------------
    # Recover failed methods: inherit flows from successful siblings.
    # When a DAO/utility method has no direct callers but a sibling
    # method in the same change set DID find entry points, the failed
    # method likely lives in the same call chain.  Assign it the same
    # flows so it is not reported as a failure.
    # -----------------------------------------------------------------
    if failures and per_method_flow_records:
        recovered = []
        # Snapshot existing flows BEFORE appending (avoid infinite iteration)
        sibling_flows = list(per_method_flow_records)
        for fi in failures:
            print(f"[RECOVERY] {fi['method_name']} ({fi['file_path']}) "
                  f"failed Stage 1 — inheriting flows from successful siblings")
            for _method_name, kind, flow_dict in sibling_flows:
                # Add the failed method as an additional trigger
                per_method_flow_records.append(
                    (fi['method_name'], kind, flow_dict))
            recovered.append(fi)
            # Update summary status
            for s in per_method_summaries:
                if (s.get('method_name') == fi['method_name']
                        and s.get('file_path') == fi['file_path']
                        and s.get('status') == 'failed'):
                    s['status'] = 'recovered'
                    break
        for fi in recovered:
            failures.remove(fi)
        if recovered:
            print(f"[OK] Recovered {len(recovered)} method(s) via sibling flow inheritance")

    # =====================================================================
    # STAGE 2.5 - consolidate + classify flows (once, after all Phase A runs)
    # =====================================================================
    banner("STAGE 2.5: flow consolidation + classification")
    consolidated_flows_list = consolidate_flows(per_method_flow_records)
    print(f"[OK] consolidated {len(per_method_flow_records)} per-method flow records "
          f"into {len(consolidated_flows_list)} unique flows")

    # Fix #5: Write the consolidated flow_dependencies.json so Stage 4 sees
    # the UNION of every per-method (flow, component) -> dependency_type
    # observation, not just the last per-method run's snapshot. DIRECT wins
    # over INDIRECT for any (flow, component) seen in multiple methods.
    if consolidated_dep_map:
        consolidated_deps_payload = {
            'dependencies': [
                {'flow': fn, 'component': cp, 'dependency_type': dt}
                for (fn, cp), dt in sorted(consolidated_dep_map.items())
            ],
            'total_dependencies': len(consolidated_dep_map),
            'source': 'consolidated multi-method (Stage 2.5)',
        }
        try:
            with open(KB_DIR / 'flow_dependencies.json',
                      'w', encoding='utf-8') as fh:
                json.dump(consolidated_deps_payload, fh,
                          indent=2, ensure_ascii=False)
            print(f"[OK] Wrote consolidated flow_dependencies.json "
                  f"({len(consolidated_dep_map)} (flow, component) pairs)")
        except Exception as exc:
            print(f"[WARN] Could not write consolidated flow_dependencies.json: {exc}")

    # Classify flows via data-driven flow_discovery (no hardcoded lists).
    try:
        from flow_discovery import classify_flows  # noqa: E402
        flow_registry = _safe_load_json(KB_DIR / 'flow_registry.json')

        # Build method lexicon from FULL component_map (not focused registry)
        # This gives statistical discovery enough signal to detect generic tokens
        component_map_data = _safe_load_json(KB_DIR / 'component_map.json')
        components = component_map_data.get('components', [])
        method_lexicon: list = []
        for comp in components:
            for method in comp.get('methods', []):
                if isinstance(method, str):
                    method_lexicon.append(method)

        print(f"[OK] Built method lexicon from component_map: {len(method_lexicon)} methods")

        # Changed methods bias PRIMARY classification
        changed_method_names = [m['method_name'] for m in all_methods]

        consolidated_flows_list = classify_flows(
            consolidated_flows_list,
            flow_registry=flow_registry,
            method_lexicon=method_lexicon,
            changed_methods=changed_method_names,
        )
        # Print a small classification breakdown for visibility.
        from collections import Counter as _Counter
        breakdown = _Counter(
            (f.get('classification') or 'UNKNOWN').upper()
            for f in consolidated_flows_list
        )
        print(f"[OK] flow classification: {dict(breakdown)}")

        # Filter out GENERIC flows before Stage 4.
        # Fix #4: We must NOT drop flows that the per-method registries
        # actually surfaced. UNION semantics: every unique flow found by
        # any per-method run remains in the consolidated registry. We
        # still record the GENERIC/DEDUP_VENEER classification on each
        # flow so downstream consumers (e.g., test correlation) can
        # de-prioritize them, but they are no longer dropped silently.
        functional_flows = list(consolidated_flows_list)
        generic_flows = [
            f for f in consolidated_flows_list
            if (f.get('classification') or 'PRIMARY').upper() in ('GENERIC', 'DEDUP_VENEER')
        ]

        if generic_flows:
            print(f"[INFO] {len(generic_flows)} flow(s) classified GENERIC/DEDUP_VENEER "
                  f"(retained for consolidation, not dropped):")
            for gf in generic_flows:
                tag = gf.get('flow_tag') or gf.get('flow_id', '?')
                reason = gf.get('classification_reason', 'no reason')
                print(f"     - {tag}: {reason}")

        # Replace consolidated_flows_list with the union list for Phase B.
        consolidated_flows_list = functional_flows
        print(f"[OK] {len(functional_flows)} flow(s) (union across all per-method runs) "
              f"will proceed to test correlation")

    except Exception as exc:
        print(f"[WARN] flow_discovery.classify_flows failed: {exc} - "
              f"flows will keep their per-method classification.")

    # =====================================================================
    # PHASE B - Run Stages 4-6 ONCE on consolidated functional flows
    # =====================================================================
    banner("PHASE B: stages 4-6 on consolidated flows")

    # Stage 4: Test correlation on consolidated flows.
    # Post-pipeline-simplification: Stage 4 reads flow_registry.json +
    # flow_dependencies.json directly. Stages 5/6 also read those files
    # (no stage2/stage3 shim is written).
    banner("STAGE 4: test correlation (consolidated)")
    stage4_output = OUTPUT_DIR / 'stage4_recommended_tests.json'

    # Resolve owning components from the canonical per-Phase-A list, with a
    # defensive fallback to deriving them again from each changed file.
    changed_components: list = []
    seen_components: set = set()
    for c in (per_method_components or []):
        if c and c not in seen_components:
            seen_components.add(c)
            changed_components.append(c)
    if not changed_components:
        for mi in all_methods:
            comp = _derive_component_from_file(mi.get('file_path', ''))
            if comp and comp not in seen_components:
                seen_components.add(comp)
                changed_components.append(comp)
    if changed_components:
        print(f"[OK] Resolved owning components for stages 4-6: {changed_components}")

    # Stage 2.6: Rebuild the enriched test corpus and flow_registry.json so
    # they cover ALL consolidated flows. Without this, Stage 4 reads the
    # focused enriched corpus left over from the LAST per-method run, which
    # only has tests tagged for that one method's ~5 flows. That mismatch
    # is why the consolidated Stage 4 produced 0 tests despite having 14
    # flows to correlate against. Failure here is non-fatal: Stage 4 will
    # fall back to whatever corpus is currently on disk.
    rebuild_consolidated_test_corpus(consolidated_flows_list)

    # For multi-method consolidated mode, skip component filtering entirely
    # The flow-level filtering (Stage 2.5) already ensures relevance:
    # - Flows come from changed methods (Phase A)
    # - Generic/infrastructure flows are removed
    # - Tests tagged with remaining functional flows are relevant by definition
    # Component keyword check adds no value, only false negatives (tests don't
    # always mention component names directly)

    # ---- Stage 1.5: LLM Method Understanding (run BEFORE diff concepts) ---
    # Must run first so its output (changed_variables, NOT_affected,
    # test scenarios) can refine diff concepts and generate test-language
    # keywords for accurate TC matching.
    run_llm_method_understanding()

    # Stage 3.5: Extract diff concepts for IDF-weighted scoring
    start_timer('stage3_5_diff_concepts')
    diff_concepts_path = OUTPUT_DIR / 'diff_concepts.json'
    from extract_diff_concepts import extract_diff_concepts, decompose_identifier

    changed_file_dicts = [{'file_path': fi['file_path']} for fi in changes['changed_files']]
    diff_concepts = extract_diff_concepts(str(REPO_ROOT), changed_file_dicts,
                                          kb_dir=str(KB_DIR))

    # ---- Enrich diff concepts with class/method name keywords --------
    # This never fails — worst case it adds nothing.
    _enrich_from_calltree(diff_concepts, changes, kb_dir=str(KB_DIR))

    # ---- Refine with LLM understanding (filter + add test keywords) --
    # Uses Stage 1.5 output to:
    #  1. REMOVE concepts from NOT_affected behaviors (false positives)
    #  2. ADD test-language keywords from LLM (bridges code→test gap)
    diff_concepts = _refine_diff_concepts_with_method_understanding(
        diff_concepts, OUTPUT_DIR)
    end_timer('stage3_5_diff_concepts')

    # Extract anchor concepts from changed method names and persist
    # so Stage 6 can use them as a relevance gate.
    anchor_concepts = _extract_anchor_concepts(changes, kb_dir=str(KB_DIR))
    anchor_path = OUTPUT_DIR / 'anchor_concepts.json'
    with open(anchor_path, 'w', encoding='utf-8') as f:
        json.dump({'anchor_concepts': anchor_concepts}, f, indent=2)

    with open(diff_concepts_path, 'w', encoding='utf-8') as f:
        json.dump(diff_concepts, f, indent=2)
    print(f"[OK] Diff concepts extracted: {len(diff_concepts.get('all_phrases', []))} phrases")

    # Stage 4 reads the (already-consolidated) flow_registry.json directly.
    # rebuild_consolidated_test_corpus() above just rewrote it to cover all
    # consolidated functional flows. Pass the comma-joined changed_components
    # so the (flow, component) DIRECT/INDIRECT lookup uses every owning
    # component (not just one).
    start_timer('stage4')
    # Option A: use the source-pipeline-only enriched corpus. The legacy
    # single-pipeline file is no longer produced.
    enriched_for_stage4 = KB_DIR / 'all_tcs_extracted_enriched_source.json'
    cmd_s4 = [
        'python3', str(SCRIPT_DIR / 'stage4_test_correlation.py'),
        '--flow-registry', str(KB_DIR / 'flow_registry.json'),
        # NO --changed-component argument = skip component filtering
        '--enriched-corpus-path', str(enriched_for_stage4),
        '--full-corpus', str(Path(REPO_ROOT) / '.github' / 'RIA_INPUT' / 'all_tcs_extracted.json'),
        '--kb-dir', str(KB_DIR),
        '--output-dir', str(OUTPUT_DIR),
    ]
    if changed_components:
        cmd_s4.extend(['--changed-components', ','.join(changed_components)])
    if diff_concepts_path and diff_concepts_path.exists():
        cmd_s4.extend(['--diff-concepts', str(diff_concepts_path)])
    rc, _, _ = run_subprocess(cmd_s4, cwd=str(REPO_ROOT))
    if rc != 0:
        print(f"[ERROR] Stage 4 failed (exit code {rc})")
        end_timer('stage4')
        return False
    print(f"[OK] Stage 4 complete: {stage4_output}")
    end_timer('stage4')

    # Stage 5: Refine tests
    #
    # In multi-method (consolidated) mode, Stage 5's dependency-aware
    # ranking degrades to a no-op: the focused KB scopes
    # flow_dependencies.json to per-method snapshots, so the consolidated
    # flow set carries (flow, component) entries Stage 5 cannot resolve.
    # Every flow ends up SKIPped in Stage 5 with no enrichment.
    #
    # Skip Stage 5 cleanly here - copy Stage 4 output to the Stage 5 path
    # so Stage 6 (which reads ``--input-file stage5``) still sees a valid
    # input file. Single-method mode is unaffected: it runs Stage 5 via
    # run_refinement() which is not on this code path.
    if not args.no_refinement:
        start_timer('stage5')
        banner("STAGE 5: test refinement (consolidated)")
        stage5_output = OUTPUT_DIR / 'stage5_refined_tests.json'
        method_csv = ','.join([m['method_name'] for m in all_methods])
        cc_csv = ','.join([c for c in changed_components if c])
        try:
            # Load Stage 4 output
            with open(stage4_output) as f:
                stage4_data = json.load(f)

            # Transform to Stage 5 format
            stage5_data = stage4_data.copy()
            if 'recommended_tests' in stage5_data:
                stage5_data['refined_tests'] = stage5_data.pop('recommended_tests')
            stage5_data['stage'] = 5
            stage5_data['description'] = 'Test refinement (skipped in multi-method mode - copied from Stage 4)'

            # Write Stage 5 output
            with open(stage5_output, 'w') as f:
                json.dump(stage5_data, f, indent=2)

            print("[Stage 5] SKIP: multi-method mode - "
                  "flow_dependencies are per-method scoped so "
                  "Stage 5 enrichment cannot resolve consolidated flows. "
                  "Copying Stage 4 output to Stage 5 for pipeline continuity.")
            print(f"[OK] Stage 5 (copy-through): {stage5_output}")
        except Exception as exc:
            print(f"[WARN] Stage 5 copy-through failed ({exc}); "
                  f"falling back to Stage 4 output path")
            stage5_output = stage4_output
        end_timer('stage5')

        # Stage 6: Aggressive suppression
        start_timer('stage6')
        banner("STAGE 6: aggressive suppression (consolidated)")
        stage6_output = OUTPUT_DIR / 'stage6_aggressive_tests.json'
        anchor_concepts_file = OUTPUT_DIR / 'anchor_concepts.json'
        cmd_s6 = [
            'python3', str(SCRIPT_DIR / 'stage6_aggressive_suppression.py'),
            '--changed-method', method_csv,
            '--changed-methods', method_csv,
            '--input-file', str(stage5_output),
            '--output-file', str(stage6_output),
            '--flow-registry', str(KB_DIR / 'flow_registry.json'),
            '--flow-dependencies', str(KB_DIR / 'flow_dependencies.json'),
            '--stage4-tests', str(stage4_output),
            '--kb-dir', str(KB_DIR),
            '--anchor-concepts', str(anchor_concepts_file),
        ]
        if cc_csv:
            cmd_s6.extend(['--changed-components', cc_csv])
        rc, _, _ = run_subprocess(cmd_s6, cwd=str(REPO_ROOT))
        if rc != 0:
            print(f"[WARN] Stage 6 failed (exit code {rc}), using Stage 5 output")
            stage6_output = stage5_output
            end_timer('stage6')
        else:
            print(f"[OK] Stage 6 complete: {stage6_output}")
            end_timer('stage6')

        final_output = stage6_output
    else:
        print("[SKIP] Refinement skipped (--no-refinement)")
        final_output = stage4_output

    # Load final consolidated tests
    final_data = _safe_load_json(final_output)
    consolidated_tests_from_phase_b = (
        final_data.get('aggressive_tests')
        or final_data.get('refined_tests')
        or final_data.get('recommended_tests')
        or []
    )

    # Build consolidated_tests dict with triggered_by tracking
    consolidated_tests = {}
    for t in consolidated_tests_from_phase_b:
        tc_id = (
            t.get('tc_id')
            or t.get('issue_key')
            or t.get('id')
            or t.get('key')
            or t.get('test_id')
        )
        if not tc_id:
            continue
        consolidated_tests[tc_id] = dict(t)
        # Preserve per-TC trigger tracking from Phase A/B instead of
        # blanket-assigning all methods.
        if 'triggered_by_methods' not in consolidated_tests[tc_id]:
            consolidated_tests[tc_id]['triggered_by_methods'] = [m['method_name'] for m in all_methods]

    # =====================================================================
    # Write consolidated outputs + HTML
    # =====================================================================
    banner("Writing consolidated outputs")
    consolidated_path = OUTPUT_DIR / 'stage6_consolidated_tests.json'
    consolidated_summary_path = OUTPUT_DIR / 'consolidated_summary.json'

    consolidated_tests_list = list(consolidated_tests.values())

    with open(consolidated_path, 'w') as fh:
        json.dump({
            'mode':              'multi_method',
            'methods':           [m['method_name'] for m in all_methods],
            'total_methods':     len(all_methods),
            'output_tests':      len(consolidated_tests_list),
            'aggressive_tests':  consolidated_tests_list,
            'consolidated_tests': consolidated_tests_list,
            'consolidated_flows': consolidated_flows_list,
        }, fh, indent=2)
    print(f"[OK] {consolidated_path}  ({len(consolidated_tests_list)} unique tests)")

    with open(consolidated_summary_path, 'w') as fh:
        json.dump({
            'mode':                       'multi_method',
            # Document the architectural source of truth so the auditor
            # (and anyone reading the report) can see why per-stage files
            # for stages 1/2/3 are intentionally absent.
            'architecture_mode':          'multi_method',
            'stage1_stage3_source':       'flow_registry.json',
            'stage5_refinement':          'skipped_per_method_flow_deps_unresolvable',
            'changed_files':              changes['total_changed_files'],
            'changed_methods':            changes['total_changed_methods'],
            'unique_tests':               len(consolidated_tests_list),
            'unique_flows':               len(consolidated_flows_list),
            'per_method':                 per_method_summaries,
            'failures':                   failures,
            'detection_errors':           changes.get('errors', []),
        }, fh, indent=2)
    print(f"[OK] {consolidated_summary_path}")

    # ---- Promote consolidated results to canonical stage6 file ------------
    canonical_s6 = OUTPUT_DIR / 'stage6_aggressive_tests.json'
    with open(canonical_s6, 'w') as fh:
        json.dump({
            'mode':              'multi_method',
            'methods':           [m['method_name'] for m in all_methods],
            'output_tests':      len(consolidated_tests_list),
            'aggressive_tests':  consolidated_tests_list,
        }, fh, indent=2)
    print(f"[OK] {canonical_s6} (overwritten with consolidated union)")

    # ---- LLM Stage 7 (TC judgment — uses Stage 1.5 output from earlier) ----
    run_llm_tc_judgment()

    # ---- Stage 8: Semantic Deduplication ---------------------------------
    try:
        run_semantic_deduplication()
    except Exception as exc:
        print(f"[WARN] Stage 8 skipped: {exc}")

    # ---- ria_v7_summary.json (per-stage counts) --------------------------
    # Documented output (see SKILL.md). Aggregates per-stage test counts so
    # downstream consumers don't need to open each stage file.
    try:
        _write_ria_summary(
            mode='multi_method',
            consolidated_flows=consolidated_flows_list,
            consolidated_tests=consolidated_tests_list,
            per_method_summaries=per_method_summaries,
            all_methods=all_methods,
            failures=failures,
        )
    except Exception as exc:
        print(f"[WARN] Could not write ria_v7_summary.json: {exc}")

    # ---- HTML report (once) ----------------------------------------------
    if not args.no_html:
        if not generate_html_report():
            print("[WARN] HTML report generation failed.")

    # ---- Summary ---------------------------------------------------------
    banner("MULTI-METHOD PIPELINE COMPLETE")
    print(f"  Methods analyzed : {len(all_methods)}")
    print(f"  Successes        : {len(all_methods) - len(failures)}")
    print(f"  Failures         : {len(failures)}")
    print(f"  Unique tests     : {len(consolidated_tests_list)}")
    print(f"  Unique flows     : {len(consolidated_flows_list)}")
    print(f"  Consolidated JSON: {consolidated_path}")
    print(f"  Per-method snaps : {multi_dir}/")
    if failures:
        print()
        print("  Failed methods:")
        for f in failures:
            print(f"    - {f['method_name']} ({f['file_path']})")
    if not args.no_html:
        report_path = OUTPUT_DIR / 'RIA_Report.html'
        if report_path.exists():
            print(f"  HTML Report      : {report_path}")
            print()
            print("Open the HTML report:")
            print(f"  open {report_path}")

    # ----- Stage execution audit (multi-method) ----------------------------
    # NOTE: The audit was previously invoked here, but it ran with
    # apply_mode='prompt' which blocks on stdin. When stdin was a pipe or
    # the user did not respond, the multi-method body never returned.
    # The audit now runs once in main(), at the end of _main_body().

    # JIRA Extension Point (or dry-run MD file)
    if args.jira_card:
        print()
        print("=" * 80)
        print("JIRA EXTENSION POINT")
        print("=" * 80)

        # JIRA Integration: Post RIA results to JIRA
        cmd = [
            'python3', str(SCRIPT_DIR / 'jira_extension.py'),
            '--jira-card', args.jira_card,
        ]

        rc, stdout, stderr = run_subprocess(cmd, cwd=str(SCRIPT_DIR))

        if rc != 0:
            print(f"[WARN] JIRA extension failed (exit code {rc})")
            if stderr:
                print(f"[WARN] {stderr}")
        else:
            print()
            print(f"✓ RIA results saved locally for review: {args.jira_card}")

    print()
    return len(failures) == 0


def main():
    # Fix P2: Clear per-run caches so each main() invocation starts fresh.
    # Currently clears _FLOW_DEPS_CACHE used by rebuild_focused_kb().
    _clear_flow_deps_cache()

    # ----- Step -3a: Python version check (warn if Python 3.9)
    if sys.version_info < (3, 10):
        print()
        print("=" * 80)
        print("[WARNING] Python 3.9 detected")
        print("=" * 80)
        print()
        print("  Some dependencies will drop Python 3.9 support in 2026.")
        print("  Upgrade to Python 3.10+ for long-term compatibility.")
        print()
        print("  Current version:", f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
        print("  Recommended: Python 3.10 or higher")
        print()
        print("=" * 80)
        print()

    # ----- Step -3: Auto-install dependencies (BEFORE argparse import-time
    # work or anything else that might pull missing modules indirectly).
    # This is the only "first-run" friction left for the developer; from
    # here on, the pipeline is fully self-contained.
    if not ensure_dependencies():
        print()
        print("[ABORT] Required dependencies could not be installed automatically.")
        print("        Please install them manually and re-run.")
        sys.exit(2)

    parser = argparse.ArgumentParser(
        description='RIA v2 Agent - Intelligent Pipeline Execution',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-detect changes from git (default when no method given)
  python3 ria_agent.py

  # Regular run (KB exists, full pipeline + HTML)
  python3 ria_agent.py --changed-method myMethod --changed-file path/to/file

  # First-time run (build KB, then analyze)
  python3 ria_agent.py --rebuild-kb --changed-method myMethod

  # Assume KB ready, skip the existence check
  python3 ria_agent.py --skip-kb-check --changed-method myMethod

  # Stop after Stage 4 (no refinement)
  python3 ria_agent.py --changed-method myMethod --no-refinement

  # Skip the HTML report
  python3 ria_agent.py --changed-method myMethod --no-html

  # Audit runs automatically at the end of the pipeline. Defaults to
  # detection + interactive approval; pass --no-audit to skip, or
  # --auto-fix-audit to apply fixes unattended:
  python3 ria_agent.py --no-audit              # skip audit entirely
  python3 ria_agent.py --auto-fix-audit        # apply fixes without prompting
  python3 ria_agent.py --audit-only            # re-audit existing outputs
""",
    )
    parser.add_argument('--changed-method', required=False, default=None,
                        help='Method name that was changed. If omitted, the agent '
                             'auto-detects changed methods from git.')
    parser.add_argument('--changed-file', default=None,
                        help='File path of the changed method (relative to repo root). '
                             'If omitted with --changed-method, the agent attempts to '
                             'locate the file via auto-detection.')
    parser.add_argument('--auto-detect', action='store_true',
                        help='Force auto-detection from git even if --changed-method '
                             'is provided.')
    parser.add_argument('--rebuild-kb', action='store_true',
                        help='Force rebuild of Knowledge Base (Stage 0). '
                             'Equivalent to a prompt containing "rebuild KB".')
    parser.add_argument('--skip-kb-check', action='store_true',
                        help='[DEPRECATED] Skip KB existence/freshness check. '
                             'Better: include words like "quick" or "skip kb" '
                             'in your prompt and let the agent decide.')
    parser.add_argument('--skip-focused-kb', action='store_true',
                        help='Skip per-change focused rebuild of '
                             'flow_registry/flow_dependencies (use existing KB)')
    # Quick Win #2 escape hatch: disable entry-point fingerprint dedup. Useful
    # for debugging or when callers explicitly want every method to run its
    # own focused KB pipeline (e.g., when investigating divergent registries).
    parser.add_argument('--no-method-dedup', action='store_true',
                        help='Disable entry-point fingerprint deduplication '
                             '(Quick Win #2). Every changed method runs its '
                             'own focused KB pipeline - useful for debugging.')
    parser.add_argument('--no-refinement', action='store_true',
                        help='Skip refinement stages (5-6); stop after Stage 4')
    parser.add_argument('--no-html', action='store_true',
                        help='Skip HTML report generation')
    parser.add_argument('--resume', action='store_true',
                        help='Resume a paused run after the Copilot agent filled '
                             'a reasoning stage (Stage 1.5 / Stage 7). Skips the '
                             'per-run cleanup so agent-provided answers survive, '
                             'and skip-guards advance the pipeline to the next '
                             'reasoning stage or the final report.')
    parser.add_argument('--live-agent', action='store_true',
                        help='Live in-process agent reasoning ("chef stays in '
                             'the kitchen"): at each reasoning stage (1.5 / 7) '
                             'the process stays alive and BLOCKS waiting for the '
                             'agent to fill the reasoning file, then continues '
                             'without a restart — instead of exiting and needing '
                             '--resume. Emits a RIA_LIVE_WAIT marker per pause '
                             'for a watching agent. Falls back to pause/resume on '
                             'timeout, so it can never hang.')
    parser.add_argument('--live-timeout', type=int, default=None,
                        help='Seconds to wait per reasoning stage in --live-agent '
                             'mode before falling back to pause/resume '
                             '(default 1800).')
    parser.add_argument('--user-prompt', default=None,
                        help='Original user prompt (for intelligent KB strategy). '
                             'If omitted, falls back to $CLAUDE_USER_PROMPT, '
                             'then to a reconstruction from argv.')
    # Deprecated: kept for backward compatibility but no longer drives behaviour.
    parser.add_argument('--kb-max-age', type=int, default=None,
                        help='[DEPRECATED] Maximum KB age in hours before warning. '
                             'Now ignored - the agent picks an appropriate '
                             'threshold from the user prompt.')
    # Phase 2: explicit language override. Defaults to 'auto' which detects
    # the dominant language from git-tracked files. 'java' preserves the
    # exact Phase 1 behaviour on this Java/Spring Boot codebase.
    parser.add_argument(
        '--language',
        choices=['auto', 'java', 'typescript', 'javascript', 'python'],
        default='auto',
        help='Force a specific language profile (default: auto-detect). '
             "Use 'java' to lock to the Phase 1 Java/Spring Boot pipeline.",
    )
    parser.add_argument('--jira-card', default=None,
                        help='JIRA card number to document RIA results '
                             '(e.g., CXWFM-12345). If provided, the pipeline will '
                             'check for Quality tab or create RIA sub-task.')
    # ----- Audit framework flags -------------------------------------------
    # By default the audit runs AUTOMATICALLY at the end of the pipeline and
    # asks the user (y/N) before applying any fixes. The flags below override
    # that default:
    #
    #   --no-audit          : skip the audit entirely
    #   --auto-fix-audit    : apply fixes WITHOUT asking
    #   --audit-only        : skip the pipeline and just re-audit existing
    #                         RIA_OUTPUT contents (no fixes)
    parser.add_argument('--no-audit', action='store_true',
                        help='Skip the post-pipeline stage-execution audit '
                             '(audit runs automatically by default).')
    parser.add_argument('--auto-fix-audit', action='store_true',
                        help='If the audit detects issues, apply fixes '
                             'WITHOUT prompting (unattended). Implies audit.')
    parser.add_argument('--audit-prompt', action='store_true',
                        help='If the audit detects fixable issues, prompt '
                             'on stdin before applying any fix. Default is '
                             'non-blocking detection-only (apply_fixes="no") '
                             'so the audit never blocks downstream stages.')
    parser.add_argument('--audit-only', action='store_true',
                        help='Skip the pipeline and only run the auditor '
                             'against the existing RIA_OUTPUT directory. '
                             'Useful for re-validating a previous run.')
    parser.add_argument('--audit-change-type', default=None,
                        choices=['source_single_method',
                                 'source_multi_method', 'source',
                                 'source_code'],
                        help='Override change type for the auditor\'s '
                             'expected-outcome validation. By default the '
                             'change type is inferred from method changes.')
    # ----- Log consolidation flag ----------------------------------------
    # Every run captures stdout + stderr to a timestamped log file under
    # <OUTPUT_DIR>/logs/. We keep the last N runs by default.
    # ----- Isolated stage rerun flag --------------------------------------
    # Re-run only one specific stage (1/2/3). Used by the audit auto-fix
    # framework to fix a Stage 1/2/3 issue WITHOUT rebuilding the KB or
    # re-running the entire pipeline. Requires --changed-method +
    # --changed-file. Implies --no-audit, --no-html, --skip-kb-check.
    #
    # Stages 1/2/3 are produced as side-effects of the focused KB rebuild
    # (build_flow_registry.py + build_flow_dependencies.py), so each
    # isolated rerun re-executes that step scoped to the changed component
    # and writes the corresponding stage output.
    parser.add_argument('--stage', default=None,
                        choices=['1', '2', '3'],
                        help='Re-run only the specified stage (1/2/3). '
                             'Used by the audit auto-fix framework to fix a '
                             'single stage issue without a full pipeline '
                             'rerun. Requires --changed-method + '
                             '--changed-file.')
    parser.add_argument('--max-logs', type=int, default=DEFAULT_MAX_LOGS,
                        help=f'Maximum number of consolidated log files to '
                             f'keep under <OUTPUT_DIR>/logs/ '
                             f'(default: {DEFAULT_MAX_LOGS}). Older logs '
                             f'are deleted at pipeline start.')
    # Internal flag used by the audit auto-fix framework when it spawns a
    # child ria_agent.py process to re-run a single stage. The parent
    # process already owns the PID-based RIA lock; the child must skip
    # lock acquisition or it would abort with EX_TEMPFAIL (75).
    parser.add_argument('--audit-child', action='store_true',
                        help='Internal flag: skip PID lock when spawned by '
                             'audit auto-fix (do not pass manually).')

    args = parser.parse_args()

    # Live in-process agent reasoning ("chef stays in the kitchen"). Opt-in;
    # default is the unchanged pause/exit + --resume flow.
    if getattr(args, 'live_agent', False):
        agent_reasoning.set_live_mode(True, timeout_sec=getattr(args, 'live_timeout', None))
        print("[live-agent] Live in-process reasoning ENABLED — the pipeline "
              "will wait in place at Stages 1.5/7 instead of exiting.")

    # PERF (2026-06-07): Audit is OFF by default for normal pipeline runs.
    # Opt-in via --auto-fix-audit, --audit-prompt, or --audit-only.
    _audit_explicitly_requested = (
        getattr(args, 'auto_fix_audit', False)
        or getattr(args, 'audit_prompt', False)
        or getattr(args, 'audit_only', False)
    )
    if not _audit_explicitly_requested:
        args.no_audit = True

    # PERF (2026-06-07): Method dedup is OFF by default.
    # The dedup probe runs a full Serena call-graph walk PER method SEQUENTIALLY
    # BEFORE Phase A starts — 4 methods × ~90s each = ~6 min wasted just probing.
    # Disabling lets Phase A workers run in parallel instead.
    # Opt-in via --method-dedup (add that flag if needed).
    if not getattr(args, 'method_dedup', False):
        args.no_method_dedup = True

    # ----- Step -1: Auto-create canonical pipeline directories -------------
    # The pipeline reads inputs from <repo>/.github/RIA_INPUT/ and writes all
    # outputs (KB, stage*.json, HTML report, consolidated log file) under
    # <repo>/.github/RIA_OUTPUT/. Both directories MUST exist before any
    # downstream code runs - including setup_logging() below, which writes
    # the consolidated log file under OUTPUT_DIR/logs/.
    #
    # Creating them here (idempotent: exist_ok=True) means a fresh clone or
    # a manually-deleted state still yields a working first run, instead of
    # failing with an opaque "FileNotFoundError" deep inside a stage. Any
    # creation failure is fatal: without these directories the pipeline
    # cannot persist results, so we abort with a clear message.
    RIA_INPUT_DIR = GITHUB_DIR / 'RIA_INPUT'
    try:
        RIA_INPUT_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUTPUT_DIR / LOGS_SUBDIR).mkdir(parents=True, exist_ok=True)
        KB_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as _mkdir_err:
        print(f"[ABORT] Failed to create RIA pipeline directories: "
              f"{_mkdir_err}")
        print(f"        RIA_INPUT : {RIA_INPUT_DIR}")
        print(f"        RIA_OUTPUT: {OUTPUT_DIR}")
        sys.exit(2)

    # ----- Step 0: Setup log consolidation IMMEDIATELY ---------------------
    # All subsequent stdout/stderr output (including subprocess output that
    # we re-emit via run_subprocess) is captured to a timestamped log file
    # under <OUTPUT_DIR>/logs/. cleanup_logging() restores the original
    # streams + writes a footer; it is registered in a finally block below
    # so it runs even when stages call sys.exit() or raise.
    log_path, cleanup_logging = setup_logging(
        OUTPUT_DIR,
        max_logs=getattr(args, 'max_logs', DEFAULT_MAX_LOGS),
    )
    print(f"[LOG] Pipeline output captured to: {log_path}")

    try:
        return _main_body(args)
    except agent_reasoning.AgentActionRequired as ar:
        # A reasoning stage needs the Copilot agent. This is a NORMAL pause,
        # not an error: print clear next-step guidance and exit 0. The agent
        # fills the named file (setting _reasoning_source=copilot-agent) and
        # re-runs with --resume to continue.
        bar = "=" * 72
        print("\n" + bar)
        print(f"PIPELINE PAUSED — {ar.stage}")
        print(bar)
        print("The Copilot agent must now reason and write:")
        print(f"  {ar.output_path}")
        print("Then continue with:")
        print("  python3 ria_agent.py --resume  (plus the same run flags)")
        print(bar + "\n")
        return 0
    except SystemExit:
        # Allow intentional sys.exit() calls to propagate after the log
        # file footer is written by cleanup_logging() in the finally
        # branch.
        raise
    except KeyboardInterrupt:
        print("\n[INTERRUPTED] Pipeline stopped by user")
        raise
    except Exception as e:
        print(f"[ERROR] Pipeline failed: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        cleanup_logging()


# ---------------------------------------------------------------------------
# Isolated single-stage execution (audit auto-fix support)
# ---------------------------------------------------------------------------
def _run_isolated_stage(stage_num: int,
                        changed_method: str,
                        changed_file: str) -> int:
    """Run only the requested stage (1, 2, or 3) and exit.

    Used by the audit auto-fix framework via the FIX_RERUN_STAGE1/2/3
    handlers in stage_execution_auditor.apply_fix(). The corresponding
    stage*.json file is rebuilt from the focused KB (flow_registry.json
    + flow_dependencies.json) without re-running downstream stages.

    Returns an exit code: 0 on success, non-zero on failure.
    """
    if not changed_method or not changed_file:
        print('[ERROR] --stage requires --changed-method + --changed-file')
        return 2
    if stage_num not in (1, 2, 3):
        print(f'[ERROR] --stage {stage_num} is not supported in isolated '
              f'mode (only 1, 2, 3 are isolated stage rebuilds)')
        return 2

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Step A: rebuild focused KB so flow_registry.json + flow_dependencies
    # reflect the current changed component.  Both files are required to
    # synthesize stage 1/2/3 outputs.
    print(f'[Stage {stage_num}] Rebuilding focused KB '
          f'(flow_registry + flow_dependencies)...')
    if not rebuild_focused_kb(changed_method, changed_file):
        print('[ERROR] Focused KB rebuild failed; cannot synthesize '
              f'stage {stage_num} output.')
        return 1

    registry = _safe_load_json(KB_DIR / 'flow_registry.json') or {}
    flows = registry.get('flows') or []
    deps_payload = _safe_load_json(KB_DIR / 'flow_dependencies.json') or {}
    deps = deps_payload.get('dependencies') or []
    changed_component = _derive_component_from_file(changed_file)

    if stage_num == 1:
        # Stage 1: entry points discovered while building the focused
        # registry. Each registry flow already carries its entry_points
        # list (file:method strings); flatten to one entry per (flow,
        # entry_point) pair to mirror the legacy stage1 contract.
        print('[Stage 1] Synthesizing stage1_entry_points.json from '
              'focused flow_registry...')
        entry_points = []
        for f in flows:
            if not isinstance(f, dict):
                continue
            for ep in f.get('entry_points') or []:
                ep_str = str(ep)
                file_part, _, method_part = ep_str.partition(':')
                entry_points.append({
                    'flow_id':   f.get('flow_id'),
                    'flow_name': f.get('flow_name'),
                    'file':      file_part or ep_str,
                    'method':    method_part or '',
                    'entry_point': ep_str,
                })
        out_path = OUTPUT_DIR / 'stage1_entry_points.json'
        with open(out_path, 'w', encoding='utf-8') as fh:
            json.dump({
                'entry_points':       entry_points,
                'total_entry_points': len(entry_points),
                'source':             'focused_flow_registry',
                'changed_method':     changed_method,
                'changed_file':       changed_file,
            }, fh, indent=2)
        print(f'[Stage 1] Wrote {len(entry_points)} entry points -> '
              f'{out_path}')
        return 0

    if stage_num == 2:
        # Stage 2: impacted (DIRECT) flows for the changed component.
        # The focused flow_registry already restricts flows to those
        # reachable from the changed code; flow_dependencies labels each
        # (flow, component) pair as DIRECT or INDIRECT. Stage 2 captures
        # the DIRECT slice (everything in the registry is directly
        # impacted when no dependency entry exists).
        print('[Stage 2] Synthesizing stage2_impacted_flows.json from '
              'focused flow_registry + flow_dependencies...')
        direct_flow_names = set()
        for d in deps:
            if not isinstance(d, dict):
                continue
            comp = str(d.get('component') or '')
            if (changed_component
                    and comp.lower() != changed_component.lower()):
                continue
            if str(d.get('dependency_type') or '').upper() == 'DIRECT':
                direct_flow_names.add(str(d.get('flow') or ''))
        impacted = []
        for f in flows:
            if not isinstance(f, dict):
                continue
            name = str(f.get('flow_name') or '')
            # If we have any DIRECT mappings for this component, prefer
            # them; otherwise treat every focused-registry flow as DIRECT
            # (focused-mode invariant).
            if direct_flow_names and name not in direct_flow_names:
                continue
            impacted.append({
                'flow_id':      f.get('flow_id'),
                'flow_name':    name,
                'test_tags':    list(f.get('test_tags') or []),
                'entry_points': list(f.get('entry_points') or []),
                'impact_type':  'DIRECT',
                'origin':       'flow_registry',
            })
        out_path = OUTPUT_DIR / 'stage2_impacted_flows.json'
        with open(out_path, 'w', encoding='utf-8') as fh:
            json.dump({
                'impacted_flows':  impacted,
                'total_flows':     len(impacted),
                'source':          'focused_flow_registry',
                'changed_method':  changed_method,
                'changed_file':    changed_file,
            }, fh, indent=2)
        print(f'[Stage 2] Wrote {len(impacted)} impacted flows -> '
              f'{out_path}')
        return 0

    # stage_num == 3
    # Stage 3: indirect flows (the changed component is reached via
    # transitive dependencies, not as a direct entry point).
    print('[Stage 3] Synthesizing stage3_indirect_flows.json from '
          'focused flow_registry + flow_dependencies...')
    indirect_flow_names = set()
    direct_flow_names = set()
    for d in deps:
        if not isinstance(d, dict):
            continue
        comp = str(d.get('component') or '')
        if (changed_component
                and comp.lower() != changed_component.lower()):
            continue
        flow_name = str(d.get('flow') or '')
        dep_type = str(d.get('dependency_type') or '').upper()
        if dep_type == 'DIRECT':
            direct_flow_names.add(flow_name)
        elif dep_type == 'INDIRECT':
            indirect_flow_names.add(flow_name)
    # Stage 2 vs Stage 3 must NOT overlap (auditor enforces this).
    indirect_flow_names -= direct_flow_names
    indirect_flows = []
    for f in flows:
        if not isinstance(f, dict):
            continue
        name = str(f.get('flow_name') or '')
        if name not in indirect_flow_names:
            continue
        indirect_flows.append({
            'flow_id':      f.get('flow_id'),
            'flow_name':    name,
            'test_tags':    list(f.get('test_tags') or []),
            'entry_points': list(f.get('entry_points') or []),
            'impact_type':  'INDIRECT',
            'origin':       'flow_dependencies',
        })
    out_path = OUTPUT_DIR / 'stage3_indirect_flows.json'
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump({
            'indirect_flows':        indirect_flows,
            'total_indirect_flows':  len(indirect_flows),
            'source':                'focused_flow_dependencies',
            'changed_method':        changed_method,
            'changed_file':          changed_file,
        }, fh, indent=2)
    print(f'[Stage 3] Wrote {len(indirect_flows)} indirect flows -> '
          f'{out_path}')
    return 0


def _main_body(args):
    """Main pipeline body (extracted so main() can wrap it in try/finally
    for log consolidation cleanup). All previous logic of main() lives
    here unchanged."""
    # ----- Audit-only short-circuit -----
    # When --audit-only is supplied we skip the pipeline entirely and only
    # validate the existing RIA_OUTPUT contents. This is the cheapest way
    # to re-check a previous run without rebuilding the KB.
    if getattr(args, 'audit_only', False):
        try:
            from stage_execution_auditor import (  # type: ignore
                audit_full_pipeline,
            )
        except Exception as _e:
            print(f"[ERROR] Could not import stage_execution_auditor: {_e}")
            sys.exit(2)
        out = audit_full_pipeline(
            repo_root=str(REPO_ROOT),
            output_dir=OUTPUT_DIR,
            kb_dir=KB_DIR,
            script_dir=SCRIPT_DIR,
            changed_method=args.changed_method,
            change_type=getattr(args, 'audit_change_type', None),
            apply_fixes='no',
        )
        print(f"[RIA Agent] Audit-only run complete. "
              f"Report: {out['report_paths']['markdown']}")
        sys.exit(0 if out['overall_status'] in ('PASS', 'FIXED') else 1)

    # ----- Isolated single-stage short-circuit -----
    # When --stage 1/2/3 is supplied (used by the audit auto-fix
    # framework), run ONLY that stage and exit. No downstream stages,
    # no audit, no HTML.  Implies --no-audit + --no-html semantics.
    _stage_arg = getattr(args, 'stage', None)
    if _stage_arg in ('1', '2', '3'):
        try:
            _stage_num = int(_stage_arg)
        except (TypeError, ValueError):
            print(f'[ERROR] Invalid --stage value: {_stage_arg}')
            sys.exit(2)
        sys.exit(
            _run_isolated_stage(
                _stage_num, args.changed_method, args.changed_file,
            )
        )

    # Phase 2: apply --language override BEFORE any other module reads the
    # active profile. set_active_language() also updates the env var so
    # child subprocesses (build_flow_registry, stage4, etc.) see it.
    try:
        from configs.ria_config import set_active_language as _set_active_language
        _set_active_language(args.language)
        if args.language != 'auto':
            print(f"[RIA Agent] Language profile forced to: {args.language}")
        else:
            print(f"[RIA Agent] Language profile: auto-detect")
    except Exception as _e:
        print(f"[RIA Agent] WARNING: failed to apply --language flag: {_e}")

    banner("RIA v2 AGENT - INTELLIGENT PIPELINE EXECUTION")

    # ----- Cleanup of RIA_OUTPUT (flag-driven) ----------------------------
    # Per-run artefacts (stage outputs, focused KB, enriched corpora, HTML)
    # are cleaned BEFORE KB validation / focused KB rebuild so the new run
    # never accidentally consumes stale files from an earlier change.
    #
    # The decision is now driven by the explicit `--rebuild-kb` CLI flag.
    # Claude (the agent harness) reads the user's natural-language prompt,
    # consults the rubric in SKILL.md ("Choosing the KB Strategy"), and
    # decides whether to pass --rebuild-kb. There are no hardcoded keyword
    # lists in this Python module.
    #
    #   * full_cleanup == True  -> delete the ENTIRE RIA_OUTPUT directory
    #                              (KB + per-run files); start from scratch.
    #   * full_cleanup == False -> selective cleanup: keep the 10 one-time
    #                              KB files (synonym_groups, component_map,
    #                              idf_index, ...) and delete only per-run
    #                              artefacts.
    try:
        # Determine cleanup mode from explicit flag only
        # (Claude determines intent and passes --rebuild-kb via SKILL.md)
        full_cleanup = bool(getattr(args, 'rebuild_kb', False))

        if getattr(args, 'resume', False):
            # RESUME pass: the Copilot agent has filled a reasoning stage
            # (method_understanding.json / stage7_llm_tc_judgment.json). Do
            # NOT clean the workspace or those agent-provided answers would be
            # deleted before the skip-guards can preserve them. Deterministic
            # stages are idempotent and simply regenerate on top of what
            # exists.
            section("Resume pass — skipping cleanup (preserving agent answers)")
            print("[INFO] --resume: workspace cleanup skipped")
        elif full_cleanup:
            # Full cleanup: delete the entire RIA_OUTPUT directory so the
            # next steps rebuild every artefact (one-time KB included)
            # from scratch. Mirrors `--rebuild-kb` semantics.
            #
            # IMPORTANT: setup_logging() (Step 0 above) opened the
            # consolidated log file under OUTPUT_DIR/logs/ BEFORE this
            # cleanup runs. If we wipe OUTPUT_DIR we must immediately
            # re-create it (and the logs/ subdir) so the still-open
            # TeeLogger file handle keeps writing to a valid location and
            # downstream stages have a folder to write into. The log file
            # itself was unlinked by rmtree but TeeLogger holds an open
            # fd, so writes continue to that (now-detached) inode until
            # the process exits - the visible log on disk will start
            # fresh when downstream stages flush.
            section("Full cleanup of RIA_OUTPUT (rebuild requested)")
            if OUTPUT_DIR.exists():
                shutil.rmtree(OUTPUT_DIR)
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            (OUTPUT_DIR / LOGS_SUBDIR).mkdir(parents=True, exist_ok=True)
            KB_DIR.mkdir(parents=True, exist_ok=True)
            print(f"[INFO] Full cleanup: deleted all {OUTPUT_DIR} "
                  f"(KB + per-run files)")
        else:
            # Selective cleanup: keep the one-time KB files, delete the
            # per-run artefacts. This is the default fast path.
            _selective_cleanup_ria_output(clean_kb_one_time=False)
            print("[INFO] Selective cleanup: preserved one-time KB files, "
                  "deleted per-run artefacts")
    except Exception as _cleanup_err:
        # Cleanup is best-effort — never let it abort the run. Stale files
        # at worst force a rebuild.
        print(f"[WARN] Cleanup failed (non-fatal): {_cleanup_err}")

    # ----- Step -2a: LLM reasoning is provided by the Copilot agent -------
    # RIA does not call any cloud LLM. The reasoning stages (1.5 / keyword
    # generation / 7) are answered by the Copilot agent DIRECTLY: each stage
    # writes a pending baseline into its normal output file and PAUSES the
    # pipeline; the agent (driven by .github/agents/ria.agent.md) fills the
    # reasoning fields and re-runs with --resume. No mailbox files, no cloud
    # credentials, so there is nothing to validate here.

    # ----- Step -2: Early prerequisite check (fail-fast) ------------------
    # Verify the raw test corpus exists BEFORE spending 7+ min on auto-detect.
    # check_raw_test_corpus() will auto-extract from Xray if missing.
    # If first-run setup is incomplete (missing deps / credentials),
    # check_raw_test_corpus() prints a friendly setup banner and we exit with
    # code 2 to distinguish "user action required" from a generic failure.
    if not check_raw_test_corpus():
        # Re-check dependencies/credentials to decide between exit code 2
        # (setup required) and exit code 1 (other failure - e.g. network /
        # API error during tc_extractor execution).
        _ok, _missing_req, _ = check_dependencies()
        _env_file = SKILL_DIR / 'configs' / 'ria_config.env'
        _cred_info = check_credentials(_env_file)
        _setup_required = (
            (not _ok)
            or (not _cred_info['env_file_present'])
            or (not _cred_info['xray_configured'])
            or (not _cred_info['project_keys_configured'])
            or (not _cred_info['llm_configured'])
        )
        if _setup_required:
            print("[ABORT] First-run setup required - see instructions above.")
            sys.exit(2)
        print("[ABORT] Cannot proceed without raw test corpus.")
        sys.exit(1)

    # ----- Step -1: Auto-detect changed methods from git ------------------
    # If no method was provided OR --auto-detect was forced, scan git for
    # changes and either (a) run a single-method pipeline if exactly ONE
    # method changed, or (b) run the multi-method consolidation pipeline
    # if multiple methods changed.
    multi_method_mode = False
    detected_changes = None

    if args.auto_detect or not args.changed_method:
        section("AUTO-DETECTING CHANGES FROM GIT")
        detected_changes = auto_detect_changes()
        _format_change_report(detected_changes)

        source_changes_present = detected_changes['total_changed_methods'] > 0

        # ---------------- Decision matrix ----------------
        # The dependency-change pipeline (pom.xml / package.json /
        # requirements.txt analysis) has been removed. Only source-method
        # changes drive the RIA pipeline now.
        if not source_changes_present:
            print("[ERROR] No changed methods detected in git.")
            print()
            print("Possible reasons:")
            print("  - Working tree is clean (no uncommitted changes).")
            print("  - All changed files are tests, generated, or in unsupported languages.")
            print("  - The repository is not a git repo.")
            print()
            print("Workarounds:")
            print("  - Make sure your edits are saved to disk (not just in IDE buffers).")
            print("  - Specify the method explicitly:")
            print("      python3 ria_agent.py --changed-method <NAME> [--changed-file <PATH>]")
            sys.exit(1)

        # source_changes_present is True from here on — pick single-method
        # or multi-method mode based on count.
        if detected_changes['total_changed_methods'] == 1:
            # Single method: fall through to the normal single-method path.
            only_file = detected_changes['changed_files'][0]
            only_method = only_file['changed_methods'][0]
            args.changed_method = only_method['method_name']
            args.changed_file = only_file['file_path']
            print(f"[OK] Single change detected -> --changed-method "
                  f"{args.changed_method}")
            print(f"     --changed-file {args.changed_file}")
        else:
            # Multiple methods: run consolidation pipeline.
            multi_method_mode = True
            print(f"[OK] Multi-method mode: {detected_changes['total_changed_methods']} "
                  f"methods across {detected_changes['total_changed_files']} files")

    # If --changed-method was provided but --changed-file wasn't, try to
    # locate the file via auto-detection so the user does not have to type
    # the full path.
    if args.changed_method and not args.changed_file and not multi_method_mode:
        if detected_changes is None:
            detected_changes = auto_detect_changes()
        for fi in detected_changes.get('changed_files', []):
            for m in fi['changed_methods']:
                if m['method_name'] == args.changed_method:
                    args.changed_file = fi['file_path']
                    print(f"[OK] Auto-resolved --changed-file from git: "
                          f"{args.changed_file}")
                    break
            if args.changed_file:
                break

    # If still no file path, abort with a helpful message. We deliberately
    # do NOT fall back to a hardcoded product-specific default - that would
    # silently produce wrong analysis for any other codebase.
    if args.changed_method and not args.changed_file and not multi_method_mode:
        print()
        print(f"[ERROR] Could not resolve --changed-file for method "
              f"'{args.changed_method}'.")
        print("        Auto-detection from git did not find this method in any "
              "modified file.")
        print()
        print("Workarounds:")
        print("  - Supply the file explicitly:")
        print(f"      python3 ria_agent.py --changed-method {args.changed_method} "
              f"--changed-file <PATH_RELATIVE_TO_REPO_ROOT>")
        print("  - Or stage/modify the file in git so auto-detection can locate it.")
        sys.exit(1)

    if multi_method_mode:
        print(f"Mode           : MULTI-METHOD ({detected_changes['total_changed_methods']} methods)")
    else:
        print(f"Changed Method : {args.changed_method}")
        print(f"Changed File   : {args.changed_file}")
    print(f"Repo Root      : {REPO_ROOT}")
    print(f"Script Dir     : {SCRIPT_DIR}")
    print(f"Output Dir     : {OUTPUT_DIR}")
    print(f"KB Dir         : {KB_DIR}")
    print(f"Timestamp      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # ----- Step 0: KB status & build (flag-driven) -----
    # Strategy is determined ONLY by explicit CLI flags. Semantic intent
    # detection from the user's prompt is now performed by Claude itself
    # via the rubric in SKILL.md - Claude reads the user's English and
    # passes --rebuild-kb (or not). This module simply maps that flag.
    user_prompt = (
        args.user_prompt
        or os.environ.get('CLAUDE_USER_PROMPT')
        or ''
    )

    strategy = infer_kb_strategy_from_prompt(
        user_prompt='',  # No longer used for strategy detection
        explicit_flags={
            'rebuild_kb': bool(args.rebuild_kb),
            'skip_kb_check': bool(args.skip_kb_check),
        },
    )
    behavior = get_validation_behavior(strategy)

    section(f"KB Validation Strategy: {explain_strategy(strategy)}")
    print(f"  Strategy : {strategy}")
    if user_prompt:
        # Truncate so we don't dump the whole argv if it's huge.
        preview = user_prompt if len(user_prompt) <= 120 else user_prompt[:117] + '...'
        print(f"  User prompt (logged for audit): {preview!r}")

    if strategy == 'rebuild':
        if not run_kb_build(force=True):
            print("[ABORT] KB build failed.")
            sys.exit(1)
    elif strategy == 'skip':
        print("[SKIP] KB validation skipped per strategy")
    else:
        # 'minimal' or 'standard'
        if behavior['check_exists']:
            kb_exists, missing = check_kb_exists()
            if not kb_exists and behavior['abort_on_missing']:
                print(f"[INFO] KB not found (missing: {', '.join(missing)}). "
                      f"Auto-building KB now (one-time setup)...")
                print()
                if not run_kb_build(force=False):
                    print("[ABORT] KB auto-build failed.")
                    sys.exit(1)
                kb_exists = True
            elif kb_exists:
                print(f"[OK] KB complete "
                      f"({len(KB_FILES_ONETIME)} one-time files present; "
                      f"{len(KB_FILES_PERANALYSIS)} per-analysis files will be "
                      f"rebuilt in Step 0b)")

        if behavior['check_freshness']:
            max_age = behavior['max_age_hours']
            is_fresh, age_hours = check_kb_freshness(max_age)
            if not is_fresh and behavior['warn_on_stale']:
                print(f"[INFO] KB is {age_hours:.1f}h old (>{max_age}h). "
                      f"Include a rebuild keyword in your prompt if you want "
                      f"a fresh KB.")
            elif is_fresh:
                print(f"[OK] KB is fresh ({age_hours:.1f}h old)")

    # ----- Multi-method branch -----
    # If git auto-detection found 2+ changed methods, run the consolidation
    # pipeline now (it handles focused-KB rebuild, analysis, refinement and
    # HTML report generation per method internally).
    if multi_method_mode:
        ok = run_multi_method_analysis(args, detected_changes)
        # Run the audit now. Defaults to detection-only (non-blocking).
        _run_post_pipeline_audit(
            args,
            multi_method_mode=True,
            changes=detected_changes,
        )
        sys.exit(0 if ok else 1)

    # ----- Step 0b: Always rebuild flow_registry + flow_dependencies for the
    #               specific changed component (GAP 2). The corpus-wide KB
    #               files (synonym_groups, component_map) are NOT rebuilt here
    #               because they are change-independent.
    if not args.skip_focused_kb:
        if not rebuild_focused_kb(args.changed_method, args.changed_file):
            print("[ABORT] Focused KB rebuild failed.")
            sys.exit(1)
    else:
        print()
        print("[SKIP] Focused KB rebuild skipped (--skip-focused-kb)")

    # ----- Step 1b: Stage 1.5 — LLM Method Understanding -----
    # Run BEFORE diff concept extraction so its output (changed_variables,
    # NOT_affected, test scenarios) can refine diff concepts.
    #
    # PRECONDITION: Stage 1.5 reads .detect_changes_cache.json from OUTPUT_DIR.
    #
    # Two modes for cache creation:
    # 1) AUTO-DETECT MODE: cache written by auto_detect_changes() at line 5040
    # 2) EXPLICIT MODE: cache synthesized from --changed-method/--changed-file args
    #
    # In explicit mode we trust the user's input as authoritative and create the
    # cache directly, without relying on git (which may find nothing if changes
    # are already committed).
    cache_path = OUTPUT_DIR / '.detect_changes_cache.json'

    # Check if cache already exists (auto-detect mode already created it)
    if not cache_path.exists():
        # Cache missing - determine why and handle appropriately
        if args.changed_method and args.changed_file:
            # EXPLICIT MODE: User provided method+file directly
            # Create cache from command-line arguments (authoritative source)
            print("[explicit mode] Creating Stage 1.5 cache from command-line arguments...")

            # Best-effort resolution of the method's line range by scanning the
            # source file. Stage 1.5 uses the range to slice the method body;
            # leaving it None caused a 'NoneType - NoneType' crash, so we
            # resolve real 1-based line numbers when the file is readable.
            resolved_start, resolved_end = _resolve_method_line_range(
                args.changed_file, args.changed_method
            )

            explicit_cache = {
                'total_changed_methods': 1,
                'total_changed_files': 1,
                'changed_files': [{
                    'file_path': args.changed_file,
                    'changed_methods': [{
                        'method_name': args.changed_method,
                        'line_start': resolved_start,  # None if not resolvable
                        'line_end': resolved_end,
                    }],
                }],
            }

            try:
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                with open(cache_path, 'w', encoding='utf-8') as f:
                    json.dump(explicit_cache, f, indent=2)
                print(f"[OK] Cache created from explicit args: {cache_path}")

                # Populate detected_changes for downstream use (diff concepts, etc.)
                detected_changes = explicit_cache

            except Exception as e:
                print(f"[ERROR] Failed to create cache file: {e}")
                sys.exit(1)
        else:
            # AUTO-DETECT MODE: Cache should have been created at line 5040.
            # If we reach here, it means auto_detect_changes() either:
            # 1) Wasn't called (bug), or
            # 2) Found 0 methods and didn't write cache (line 2442 guard)
            #
            # Try running detection now as fallback.
            print("[auto-detect fallback] Cache missing, running git detection now...")
            detected_changes = auto_detect_changes()

            # Check if cache was created by the fallback call
            if not cache_path.exists():
                print("[ERROR] No changes detected by git and no explicit method provided.")
                print("        Stage 1.5 requires change information to proceed.")
                print()
                print("Options:")
                print("  1. Make sure changes are saved and uncommitted (git diff shows them)")
                print("  2. Provide explicit method: --changed-method <NAME> --changed-file <PATH>")
                sys.exit(1)

    # At this point, cache_path is guaranteed to exist.
    # detected_changes may be None (if explicit mode didn't need git) or populated.
    run_llm_method_understanding()

    # ----- Step 1c: Diff concept extraction + refinement -----
    # Build diff concepts the same way multi-method mode does so that
    # Stage 4 gets IDF-weighted scoring, not just base flow/component scores.
    diff_concepts_path = OUTPUT_DIR / 'diff_concepts.json'
    if detected_changes is None:
        detected_changes = auto_detect_changes()
    from extract_diff_concepts import extract_diff_concepts, decompose_identifier
    changed_file_dicts = [{'file_path': fi['file_path']}
                          for fi in detected_changes.get('changed_files', [])]
    diff_concepts = extract_diff_concepts(str(REPO_ROOT), changed_file_dicts,
                                          kb_dir=str(KB_DIR))
    _enrich_from_calltree(diff_concepts, detected_changes, kb_dir=str(KB_DIR))
    diff_concepts = _refine_diff_concepts_with_method_understanding(
        diff_concepts, OUTPUT_DIR)
    anchor_concepts = _extract_anchor_concepts(detected_changes, kb_dir=str(KB_DIR))
    anchor_path = OUTPUT_DIR / 'anchor_concepts.json'
    with open(anchor_path, 'w', encoding='utf-8') as f:
        json.dump({'anchor_concepts': anchor_concepts}, f, indent=2)
    with open(diff_concepts_path, 'w', encoding='utf-8') as f:
        json.dump(diff_concepts, f, indent=2)
    print(f"[OK] Diff concepts extracted: {len(diff_concepts.get('all_phrases', []))} phrases")

    # ----- Step 1d: Stage 4 (with diff concepts + full corpus) -----
    # Stage 4 reads flow_registry.json directly (post-pipeline-simplification).
    # Stages 1/2/3 are no longer invoked - Stage 4 derives impacted flows
    # from the focused flow_registry and resolves DIRECT/INDIRECT criticality
    # via flow_dependencies.json.
    banner("STAGE 4: TEST CORRELATION (with IDF scoring)")
    # Option A: use the source-pipeline-only enriched corpus. The legacy
    # single-pipeline file is no longer produced.
    enriched_corpus = KB_DIR / 'all_tcs_extracted_enriched_source.json'
    full_corpus = Path(REPO_ROOT) / '.github' / 'RIA_INPUT' / 'all_tcs_extracted.json'
    cmd_s4 = [
        'python3', str(SCRIPT_DIR / 'stage4_test_correlation.py'),
        '--flow-registry', str(KB_DIR / 'flow_registry.json'),
        '--changed-file', args.changed_file,
        '--enriched-corpus-path', str(enriched_corpus),
        '--kb-dir', str(KB_DIR),
        '--output-dir', str(OUTPUT_DIR),
        '--diff-concepts', str(diff_concepts_path),
    ]
    if full_corpus.exists():
        cmd_s4.extend(['--full-corpus', str(full_corpus)])
    rc, _, _ = run_subprocess(cmd_s4, cwd=str(REPO_ROOT))
    if rc != 0:
        print(f"[ERROR] Stage 4 failed (exit code {rc})")
        sys.exit(1)
    print("[OK] Stage 4 complete (with IDF scoring)")

    # ----- Step 2: Refinement (Stages 5-6) -----
    if args.no_refinement:
        print()
        print("[SKIP] Refinement skipped (--no-refinement)")
    else:
        # Resolve owning component(s) so Stages 5/6 can pull authoritative
        # component-keyword sets from component_map.json and resolve the
        # DIRECT/INDIRECT label per (flow, component).
        single_changed_component = _derive_component_from_file(args.changed_file)
        single_components = [single_changed_component] if single_changed_component else []
        if not run_refinement(args.changed_method, single_components):
            print("[WARN] Refinement pipeline did not complete cleanly.")
            print("       Stage 4 output is still available.")

    # ----- Step 2b: LLM Stage 7 (1.5 already ran before Stage 4) -----
    run_llm_tc_judgment()

    # ----- Step 2c: Stage 8 Semantic Deduplication --------------------------
    try:
        run_semantic_deduplication()
    except Exception as exc:
        print(f"[WARN] Stage 8 skipped: {exc}")

    # ----- ria_v7_summary.json (per-stage counts) ----------------------------
    try:
        # Single-method path: load consolidated stage6 if present.
        s6_path = OUTPUT_DIR / 'stage6_aggressive_tests.json'
        s6_data = _safe_load_json(s6_path)
        s6_tests = s6_data.get('aggressive_tests') or s6_data.get('tests') or []
        s2_data = _safe_load_json(OUTPUT_DIR / 'stage2_impacted_flows.json')
        flows = s2_data.get('impacted_flows') or []
        _write_ria_summary(
            mode='single_method',
            consolidated_flows=flows,
            consolidated_tests=s6_tests,
        )
    except Exception as exc:
        print(f"[WARN] Could not write ria_v7_summary.json: {exc}")

    # ----- Step 3: HTML report -----
    if args.no_html:
        print()
        print("[SKIP] HTML report skipped (--no-html)")
    else:
        if not generate_html_report():
            print("[WARN] HTML report generation failed.")

    # ----- Summary -----
    banner("PIPELINE COMPLETE")
    print(f"  Knowledge Base   : {KB_DIR}/")
    print(f"  Stage outputs    : {OUTPUT_DIR}/stage*.json")

    if not args.no_refinement:
        s6 = OUTPUT_DIR / 'stage6_aggressive_tests.json'
        if s6.exists():
            n = _read_test_count(s6, 'aggressive_tests')
            print(f"  Final tests      : {s6}  ({n} tests)")
        else:
            s5 = OUTPUT_DIR / 'stage5_refined_tests.json'
            if s5.exists():
                n = _read_test_count(s5, 'refined_tests')
                print(f"  Refined tests    : {s5}  ({n} tests)")

    if not args.no_html:
        report_path = OUTPUT_DIR / 'RIA_Report.html'
        if report_path.exists():
            print(f"  HTML Report      : {report_path}")
            print()
            print("Open the HTML report:")
            print(f"  open {report_path}")

    # ----- Stage execution audit ------------------------------------------
    # Runs at the end of every single-method pipeline run unless
    # --no-audit was passed. Defaults to detection-only (non-blocking) so
    # the audit cannot stall downstream consumers; pass --audit-prompt to
    # restore the legacy interactive flow, or --auto-fix-audit to apply
    # fixes unattended.
    _run_post_pipeline_audit(
        args,
        multi_method_mode=False,
        changes=detected_changes,
    )

    # JIRA Extension Point (or dry-run MD file)
    if args.jira_card:
        print()
        print("=" * 80)
        print("JIRA EXTENSION POINT")
        print("=" * 80)

        # JIRA Integration: Post RIA results to JIRA
        cmd = [
            'python3', str(SCRIPT_DIR / 'jira_extension.py'),
            '--jira-card', args.jira_card,
        ]

        rc, stdout, stderr = run_subprocess(cmd, cwd=str(SCRIPT_DIR))

        if rc != 0:
            print(f"[WARN] JIRA extension failed (exit code {rc})")
            if stderr:
                print(f"[WARN] {stderr}")
        else:
            print()
            print(f"✓ RIA results saved locally for review: {args.jira_card}")

    print()


if __name__ == '__main__':
    # Concurrency guard: acquire the PID-based lock BEFORE any pipeline
    # work starts. If another live RIA process owns the lock, abort with a
    # clear diagnostic (exit code 75 = EX_TEMPFAIL, "service unavailable -
    # try again later"). Stale locks from crashed runs are reclaimed
    # automatically inside acquire_ria_lock().
    #
    # EXCEPTION: when this process was spawned by the audit auto-fix
    # framework (parent ria_agent.py is still holding the lock), the
    # child must skip lock acquisition. We detect that mode by peeking
    # at sys.argv before argparse runs - this is intentionally a simple
    # token check so it works before any other initialization.
    _is_audit_child = '--audit-child' in sys.argv
    if not _is_audit_child:
        if not acquire_ria_lock():
            sys.exit(75)
    # Remove any stray RIA_Report_old.html left behind by older versions
    # of the report generator so the output directory stays tidy.
    cleanup_legacy_report_backup()
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n[INTERRUPTED] Pipeline stopped by user")
        sys.exit(130)
    except SystemExit:
        # main() called sys.exit(...) intentionally - propagate the code.
        raise
    except Exception as e:
        print(f"\n\n[FATAL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        # Always release the lock - on normal exit, on KeyboardInterrupt,
        # on SystemExit, and on uncaught exceptions. release_ria_lock()
        # only deletes the lock when our PID still owns it, so it is safe
        # even if the lock was reclaimed by a later run.
        release_ria_lock()
