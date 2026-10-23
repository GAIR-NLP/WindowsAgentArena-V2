"""Copy declared actual outputs without saving applications or invoking metrics."""
import datetime
import json
from pathlib import Path, PureWindowsPath
import time

from model_agent import PhaseDeadline


CHANGE_ID = "waa-finalization-artifact-export-20261023-r1"
EXPORT_TIMEOUT_SECONDS = 30.0


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def declared_files(task):
    # Only actual-result declarations are eligible; never inspect expected/gold.
    config = task.get("evaluator", {}).get("result")
    configs = config if isinstance(config, list) else [config]
    files = []
    for getter_index, entry in enumerate(configs):
        if not isinstance(entry, dict) or entry.get("type") != "vm_file":
            continue
        paths = entry.get("path") if entry.get("multi") else [entry.get("path")]
        dests = entry.get("dest") if entry.get("multi") else [entry.get("dest")]
        if (entry.get("time_suffix") or not isinstance(paths, list)
                or not isinstance(dests, list) or len(paths) != len(dests)
                or not paths or any(not isinstance(p, str) or not p for p in paths)
                or any(not isinstance(d, str) or not d for d in dests)):
            files.append({"getter_index": getter_index, "status": "invalid_config",
                          "error": "Expected paired literal vm_file paths/dests without time_suffix"})
            continue
        for path, dest in zip(paths, dests):
            name = PureWindowsPath(dest).name
            if name in {"", ".", ".."}:
                files.append({"getter_index": getter_index, "status": "invalid_config",
                              "error": "Output dest has no filename"})
                continue
            files.append({"getter_index": getter_index, "guest_path": path,
                          "declared_dest": dest, "filename": name})
    return files


def export_phase(controller, task, folder, identity, stage, deadline,
                 *, state_stability, timeout=EXPORT_TIMEOUT_SECONDS):
    """Use one total time budget, shared fairly across independent reads."""
    if stage not in {"pre-evaluator", "post-evaluator"}:
        raise ValueError("Unknown artifact stage")
    folder = Path(folder)
    root = folder / "artifacts" / stage
    report = {"change_id": CHANGE_ID, **identity, "stage": stage,
              "state_stability": state_stability, "started_at": timestamp(),
              "timeout_seconds": timeout, "files": declared_files(task)}
    cutoff = time.monotonic() + timeout
    pending = [item for item in report["files"] if "status" not in item]
    screenshot = {"kind": "screenshot"}
    operations = pending + [screenshot]
    report["screenshot"] = screenshot
    interruption = None
    for index, item in enumerate(operations):
        remaining = cutoff - time.monotonic()
        item["started_at"] = timestamp()
        if remaining <= 0:
            item.update(status="timeout", error="Export phase budget exhausted")
            item["finished_at"] = timestamp()
            continue
        budget = remaining / (len(operations) - index)
        target = (folder / "final.png" if stage == "pre-evaluator" else root / "desktop.png") \
            if item is screenshot else root / "files" / f"{index:03d}-{item['filename']}"
        item["host_path"] = str(target)
        item["timeout_seconds"] = budget
        temporary = target.with_name(target.name + ".partial")
        try:
            with deadline(budget):
                data = controller.get_screenshot() if item is screenshot else controller.get_file(item["guest_path"])
                item["read_finished_at"] = timestamp()
                if data is None or (item is screenshot and data == b""):
                    item.update(status="unavailable", error="Controller returned no bytes; cause is not classified as missing")
                elif not isinstance(data, bytes):
                    item.update(status="error", error="Controller returned a non-bytes payload")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with temporary.open("xb") as stream:
                        stream.write(data)
                    temporary.replace(target)
                    item.update(status="saved", bytes=len(data), persisted_at=timestamp())
        except (PhaseDeadline, TimeoutError) as exc:
            item.update(status="timeout", error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            item.update(status="error", error=f"{type(exc).__name__}: {exc}")
        except BaseException as exc:
            interruption = exc
            item.update(status="interrupted", error=f"{type(exc).__name__}: {exc}")
            report["status"] = "interrupted"
        finally:
            item["finished_at"] = timestamp()
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                item["partial_cleanup_error"] = str(exc)
        if interruption is not None:
            for later in operations[index + 1:]:
                later.update(status="not_attempted", reason="Export phase interrupted")
            break
    report["finished_at"] = timestamp()
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    except Exception as exc:
        # The caller also embeds the report in result.json, before owner cleanup.
        report["manifest_error"] = f"{type(exc).__name__}: {exc}"
    except BaseException as exc:
        report["manifest_error"] = f"{type(exc).__name__}: {exc}"
        report["status"] = "interrupted"
        if interruption is None:
            interruption = exc
    if interruption is not None:
        interruption.artifact_export_report = report
        raise interruption
    return report
