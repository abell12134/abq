"""MinIO 数据同步：origin 服务器只上传；client 本地启动时拉取。

同步内容：
  1. Qlib 行情包 datasets/qlib_data/cn_data（打包为 qlib/cn_data.tar.gz）
  2. quant/data/ 运行时目录（signals、accounts、overlays 等，不含 logs）

配置：configs/global.yaml 的 minio 段 + configs/secret.env 凭证。
  role=origin（默认，生产服务器）：只 push，启动/ensure_qlib 不 pull
  role=client（本地开发机，secret.env 设 MINIO_ROLE=client）：启动时 pull

用法：
    python quant/ops/minio_sync.py status
    python quant/ops/minio_sync.py pull          # 仅 client；origin 默认跳过
    python quant/ops/minio_sync.py pull --force  # origin 上紧急拉取
    python quant/ops/minio_sync.py push          # 上传本地数据到 MinIO
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

REPO = Path(__file__).resolve().parents[2]
QUANT = REPO / "quant"
CFG_PATH = QUANT / "configs" / "global.yaml"
SECRET_PATH = QUANT / "configs" / "secret.env"
DATA_ROOT = QUANT / "data"
QLIB_DATA = REPO / "datasets" / "qlib_data" / "cn_data"
MANIFEST_KEY = "abq-sync/manifest.json"
TZ = ZoneInfo("Asia/Shanghai")

DEFAULT_QUANT_PATHS = (
    "signals",
    "accounts",
    "overlays",
    "agent",
    "meta",
    "reports",
    "nav",
    "orders",
    "fills",
    "target_position",
    "csv_raw",
)


def _load_secret() -> dict[str, str]:
    env: dict[str, str] = {}
    if SECRET_PATH.exists():
        for line in SECRET_PATH.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _cfg() -> dict:
    return yaml.safe_load(CFG_PATH.read_text())


def minio_settings() -> dict[str, Any] | None:
    """合并 global.yaml 与 secret.env；未启用或缺凭证时返回 None。"""
    cfg = _cfg().get("minio") or {}
    if not cfg.get("enabled", False):
        return None
    secret = _load_secret()
    access = secret.get("MINIO_ACCESS_KEY") or os.environ.get("MINIO_ACCESS_KEY")
    secret_key = secret.get("MINIO_SECRET_KEY") or os.environ.get("MINIO_SECRET_KEY")
    if not access or not secret_key:
        return None
    endpoint = (
        secret.get("MINIO_ENDPOINT")
        or os.environ.get("MINIO_ENDPOINT")
        or cfg.get("endpoint", "118.195.177.58:9000")
    )
    role = (
        os.environ.get("MINIO_ROLE")
        or secret.get("MINIO_ROLE")
        or cfg.get("role")
        or "origin"
    ).strip().lower()
    if role not in {"origin", "client", "both"}:
        role = "origin"
    allow_pull = role in {"client", "both"}
    return {
        "endpoint": endpoint,
        "access_key": access,
        "secret_key": secret_key,
        "bucket": cfg.get("bucket", "abq-data"),
        "secure": bool(cfg.get("secure", False)),
        "qlib_object": cfg.get("qlib_object", "qlib/cn_data.tar.gz"),
        "quant_prefix": cfg.get("quant_data_prefix", "quant-data/"),
        "quant_paths": list(cfg.get("quant_data_paths") or DEFAULT_QUANT_PATHS),
        "role": role,
        "allow_pull": allow_pull,
        "sync_on_startup": bool(cfg.get("sync_on_startup", False)) and allow_pull,
    }


def _client():
    from minio import Minio

    s = minio_settings()
    if not s:
        raise RuntimeError("MinIO 未启用或缺少凭证（configs/secret.env）")
    return Minio(
        s["endpoint"],
        access_key=s["access_key"],
        secret_key=s["secret_key"],
        secure=s["secure"],
    ), s


def _ensure_bucket(client, bucket: str) -> None:
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


def local_qlib_last_date(data_dir: Path | None = None) -> str | None:
    cal = (data_dir or QLIB_DATA) / "calendars" / "day.txt"
    if not cal.exists():
        return None
    return cal.read_text().strip().splitlines()[-1]


def read_manifest(client, bucket: str) -> dict | None:
    try:
        resp = client.get_object(bucket, MANIFEST_KEY)
        try:
            return json.loads(resp.read().decode("utf-8"))
        finally:
            resp.close()
            resp.release_conn()
    except Exception:
        return None


def write_manifest(client, bucket: str, manifest: dict) -> None:
    body = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    from io import BytesIO

    client.put_object(
        bucket,
        MANIFEST_KEY,
        BytesIO(body),
        length=len(body),
        content_type="application/json",
    )


def _now_iso() -> str:
    return dt.datetime.now(TZ).isoformat()


def needs_qlib_pull(remote: dict | None, data_dir: Path | None = None) -> bool:
    if not remote:
        return False
    qlib = remote.get("qlib") or {}
    remote_date = qlib.get("last_date")
    if not remote_date:
        return False
    local_date = local_qlib_last_date(data_dir)
    if not local_date:
        return True
    return remote_date > local_date


def _atomic_swap(new_dir: Path, data_dir: Path) -> None:
    bak = data_dir.with_suffix(".bak")
    if bak.exists():
        shutil.rmtree(bak)
    if data_dir.exists():
        data_dir.rename(bak)
    shutil.move(str(new_dir), str(data_dir))
    if bak.exists():
        shutil.rmtree(bak, ignore_errors=True)


def _sanity_check_qlib(data_dir: Path) -> tuple[bool, list[str]]:
    problems: list[str] = []
    cal = data_dir / "calendars" / "day.txt"
    if not cal.exists():
        return False, ["缺少 calendars/day.txt"]
    for f in ("instruments/all.txt", "features/sh600000/close.day.bin"):
        if not (data_dir / f).exists():
            problems.append(f"缺少 {f}")
    feat = data_dir / "features"
    if feat.exists():
        n = sum(1 for _ in feat.iterdir())
        if n < 5000:
            problems.append(f"features 仅 {n} 只标的，疑似不完整")
    return not problems, problems


def pull_qlib(*, force: bool = False) -> dict[str, Any]:
    """从 MinIO 拉取 Qlib 行情包并原子替换本地 cn_data。"""
    client, s = _client()
    if not s.get("allow_pull") and not force:
        return {
            "ok": True,
            "action": "skip",
            "reason": f"role={s.get('role')} 本机是数据源，不从 MinIO 拉取",
        }
    bucket = s["bucket"]
    obj = s["qlib_object"]
    data_dir = QLIB_DATA

    remote = read_manifest(client, bucket)
    if not force and not needs_qlib_pull(remote, data_dir):
        local = local_qlib_last_date(data_dir)
        remote_date = (remote or {}).get("qlib", {}).get("last_date")
        return {
            "ok": True,
            "action": "skip",
            "reason": f"本地({local})已是最新(远端{remote_date})",
        }

    print(f"[minio] 拉取 {obj} …")
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=data_dir.parent) as tmp:
        work = Path(tmp)
        tarball = work / "cn_data.tar.gz"
        client.fget_object(bucket, obj, str(tarball))

        extract_root = work / "extracted"
        extract_root.mkdir()
        with tarfile.open(tarball) as tf:
            tf.extractall(extract_root)

        # 支持 cn_data/ 或 qlib_bin/ 两种打包格式
        inner = extract_root / "cn_data"
        if not inner.exists():
            inner = extract_root / "qlib_bin"
        if not inner.exists():
            inner = next(extract_root.iterdir(), None)
        if inner is None or not inner.is_dir():
            return {"ok": False, "error": "压缩包内未找到 cn_data 目录"}

        ok, problems = _sanity_check_qlib(inner)
        if not ok:
            return {"ok": False, "error": "校验失败", "problems": problems}

        staging = work / "staging"
        shutil.move(str(inner), str(staging))
        _atomic_swap(staging, data_dir)
        marker = data_dir / ".abq_data_ready"
        marker.write_text(f"synced_from_minio at={_now_iso()}\n")

    last = local_qlib_last_date(data_dir)
    print(f"[minio] Qlib 已同步至 {last}")
    return {"ok": True, "action": "pulled", "last_date": last}


def push_qlib() -> dict[str, Any]:
    """打包本地 cn_data 并上传到 MinIO，更新 manifest。"""
    if not QLIB_DATA.exists():
        return {"ok": False, "error": f"本地数据不存在: {QLIB_DATA}"}

    ok, problems = _sanity_check_qlib(QLIB_DATA)
    if not ok:
        return {"ok": False, "error": "本地数据校验失败", "problems": problems}

    client, s = _client()
    bucket = s["bucket"]
    obj = s["qlib_object"]
    _ensure_bucket(client, bucket)

    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "cn_data.tar.gz"
        print(f"[minio] 打包 {QLIB_DATA} …")
        with tarfile.open(tarball, "w:gz") as tf:
            tf.add(QLIB_DATA, arcname="cn_data")
        print(f"[minio] 上传 {obj} ({tarball.stat().st_size / 1e6:.0f} MB) …")
        client.fput_object(bucket, obj, str(tarball))

    manifest = read_manifest(client, bucket) or {"version": 1}
    manifest["qlib"] = {
        "last_date": local_qlib_last_date(QLIB_DATA),
        "updated_at": _now_iso(),
        "object": obj,
    }
    write_manifest(client, bucket, manifest)
    print(f"[minio] Qlib 已上传（截止 {manifest['qlib']['last_date']}）")
    return {"ok": True, "action": "pushed", "last_date": manifest["qlib"]["last_date"]}


def _remote_key(prefix: str, rel: Path) -> str:
    return f"{prefix}{rel.as_posix()}"


def pull_quant_data(*, force: bool = False) -> dict[str, Any]:
    """拉取 quant/data 子目录（跳过 logs）。"""
    client, s = _client()
    if not s.get("allow_pull") and not force:
        return {
            "ok": True,
            "action": "skip",
            "reason": f"role={s.get('role')} 本机是数据源，不从 MinIO 拉取",
        }
    bucket = s["bucket"]
    prefix = s["quant_prefix"]
    paths = s["quant_paths"]
    downloaded = 0

    for sub in paths:
        local_base = DATA_ROOT / sub
        remote_prefix = f"{prefix}{sub}/"
        for obj in client.list_objects(bucket, prefix=remote_prefix, recursive=True):
            if obj.object_name.endswith("/"):
                continue
            rel = Path(obj.object_name[len(prefix):])
            local = DATA_ROOT / rel
            if (
                not force
                and local.exists()
                and obj.last_modified
                and local.stat().st_mtime >= obj.last_modified.timestamp() - 1
            ):
                continue
            local.parent.mkdir(parents=True, exist_ok=True)
            client.fget_object(bucket, obj.object_name, str(local))
            downloaded += 1

    print(f"[minio] quant/data 拉取完成，更新 {downloaded} 个文件")
    return {"ok": True, "action": "pulled", "files": downloaded}


def push_quant_data() -> dict[str, Any]:
    """上传 quant/data 子目录到 MinIO。"""
    client, s = _client()
    bucket = s["bucket"]
    prefix = s["quant_prefix"]
    paths = s["quant_paths"]
    _ensure_bucket(client, bucket)
    uploaded = 0

    for sub in paths:
        local_base = DATA_ROOT / sub
        if not local_base.exists():
            continue
        for fp in local_base.rglob("*"):
            if not fp.is_file():
                continue
            rel = fp.relative_to(DATA_ROOT)
            key = _remote_key(prefix, rel)
            client.fput_object(bucket, key, str(fp))
            uploaded += 1

    manifest = read_manifest(client, bucket) or {"version": 1}
    manifest["quant_data"] = {"updated_at": _now_iso(), "prefix": prefix}
    write_manifest(client, bucket, manifest)
    print(f"[minio] quant/data 已上传 {uploaded} 个文件")
    return {"ok": True, "action": "pushed", "files": uploaded}


def sync_on_startup(*, force: bool = False) -> dict[str, Any]:
    """启动时调用：仅 client 角色拉取。origin 服务器跳过。失败不抛异常。"""
    s = minio_settings()
    if not s or not s.get("allow_pull") or not s.get("sync_on_startup"):
        role = (s or {}).get("role", "origin")
        return {"ok": True, "action": "disabled", "reason": f"role={role} 不在启动时拉取"}

    results: dict[str, Any] = {"ok": True, "parts": {}}
    try:
        results["parts"]["qlib"] = pull_qlib(force=force)
    except Exception as exc:
        results["parts"]["qlib"] = {"ok": False, "error": str(exc)}
        print(f"[minio] Qlib 同步失败（继续启动）: {exc}", file=sys.stderr)

    try:
        results["parts"]["quant_data"] = pull_quant_data(force=force)
    except Exception as exc:
        results["parts"]["quant_data"] = {"ok": False, "error": str(exc)}
        print(f"[minio] quant/data 同步失败（继续启动）: {exc}", file=sys.stderr)

    results["ok"] = all(
        p.get("ok", False) or p.get("action") == "skip"
        for p in results["parts"].values()
    )
    return results


def status() -> dict[str, Any]:
    s = minio_settings()
    if not s:
        return {"enabled": False, "reason": "未启用或缺少凭证"}

    out: dict[str, Any] = {
        "enabled": True,
        "endpoint": s["endpoint"],
        "bucket": s["bucket"],
        "local_qlib_last": local_qlib_last_date(),
        "role": s["role"],
        "allow_pull": s["allow_pull"],
        "sync_on_startup": s["sync_on_startup"],
    }
    try:
        client, _ = _client()
        remote = read_manifest(client, s["bucket"])
        out["remote_qlib_last"] = (remote or {}).get("qlib", {}).get("last_date")
        out["remote_qlib_updated"] = (remote or {}).get("qlib", {}).get("updated_at")
        out["needs_qlib_pull"] = needs_qlib_pull(remote)
    except Exception as exc:
        out["error"] = str(exc)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="MinIO 数据同步")
    ap.add_argument("action", choices=["status", "pull", "push"])
    ap.add_argument("--force", action="store_true", help="忽略版本检查强制拉取")
    ap.add_argument("--qlib-only", action="store_true")
    ap.add_argument("--data-only", action="store_true")
    args = ap.parse_args()

    if args.action == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0

    if not minio_settings():
        print("[FATAL] MinIO 未启用，请配置 global.yaml minio.enabled 与 secret.env", file=sys.stderr)
        return 1

    try:
        if args.action == "pull":
            if args.data_only:
                r = pull_quant_data(force=args.force)
            elif args.qlib_only:
                r = pull_qlib(force=args.force)
            else:
                r = sync_on_startup(force=args.force)
        else:
            parts = {}
            if not args.data_only:
                parts["qlib"] = push_qlib()
            if not args.qlib_only:
                parts["quant_data"] = push_quant_data()
            r = {"ok": True, "parts": parts}
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok", True) else 1
    except Exception as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
