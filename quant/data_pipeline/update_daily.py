"""每日数据更新管道（主路径）：下载 investment_data 每日发布的 Qlib 数据包，
校验后原子替换本地数据。

    python update_daily.py            # 自动取最新 release
    python update_daily.py --tag 2026-06-11

为什么不用 baostock 逐股全量抓取：6000+ 只股票全量抓取耗时不可行，且成分股为当前
时点名单存在幸存者偏差。故主路径用此 release 包；当上游停更时，由 update_incremental.py
用 baostock 只对「当前标的池」增量补到最新交易日作为回退（run_daily 已自动串联）。

数据源：https://github.com/chenditc/investment_data （每日更新，含历史时点成分股）

校验不通过即保留旧数据并退出非零码（宁可用昨天的数据停一天，不可换入坏数据）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "global.yaml"
RELEASE_API = "https://api.github.com/repos/chenditc/investment_data/releases/latest"
DOWNLOAD_URL = "https://github.com/chenditc/investment_data/releases/download/{tag}/qlib_bin.tar.gz"


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def latest_release_tag() -> str:
    req = urllib.request.Request(RELEASE_API, headers={"User-Agent": "quant-pipeline"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["tag_name"]


def local_last_date(data_dir: Path) -> str | None:
    cal = data_dir / "calendars" / "day.txt"
    if not cal.exists():
        return None
    return cal.read_text().strip().splitlines()[-1]


def download_and_extract(tag: str, work: Path) -> Path:
    url = DOWNLOAD_URL.format(tag=tag)
    tarball = work / "qlib_bin.tar.gz"
    print(f"[1/3] 下载 {url}")
    subprocess.run(
        ["curl", "-sL", "--max-time", "900", "-o", str(tarball), url], check=True)
    extract_dir = work / "extracted"
    extract_dir.mkdir()
    with tarfile.open(tarball) as tf:
        tf.extractall(extract_dir)
    # 包内顶层目录名为 qlib_bin
    inner = next(extract_dir.iterdir())
    return inner


def sanity_check(new_dir: Path, old_dir: Path, tag: str) -> tuple[bool, list[str]]:
    problems = []
    cal = new_dir / "calendars" / "day.txt"
    if not cal.exists():
        return False, ["新数据缺少 calendars/day.txt"]
    new_last = cal.read_text().strip().splitlines()[-1]
    # release tag 即数据截止日，新数据日历必须达到 tag 当日（容差1个交易日）
    if (dt.date.fromisoformat(tag) - dt.date.fromisoformat(new_last)).days > 3:
        problems.append(f"新数据日历截止 {new_last}，远落后于 release {tag}")

    old_last = local_last_date(old_dir)
    if old_last and new_last < old_last:
        problems.append(f"新数据 {new_last} 比现有数据 {old_last} 更旧")

    for f in ("instruments/all.txt", "instruments/csi300.txt",
              "features/sh600000/close.day.bin"):
        if not (new_dir / f).exists():
            problems.append(f"新数据缺少 {f}")

    n_feat = sum(1 for _ in (new_dir / "features").iterdir())
    if n_feat < 5000:
        problems.append(f"features 目录仅 {n_feat} 只标的，疑似不完整")
    return not problems, problems


def atomic_swap(new_dir: Path, data_dir: Path) -> None:
    bak = data_dir.with_suffix(".bak")
    if bak.exists():
        shutil.rmtree(bak)
    if data_dir.exists():
        data_dir.rename(bak)
    shutil.move(str(new_dir), str(data_dir))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default=None)
    parser.add_argument(
        "--force", action="store_true",
        help="即使本地日历已覆盖 tag 也强制重新下载替换（修复残缺 release）",
    )
    args = parser.parse_args()

    cfg = load_config()
    quant_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(quant_root / "ops"))
    from ensure_qlib_data import resolve_provider_uri
    # update_daily 会整体替换数据目录；指向解压后的 cn_data
    data_dir = Path(resolve_provider_uri(cfg["paths"]["qlib_data"], ensure=True))

    tag = args.tag or latest_release_tag()
    old_last = local_last_date(data_dir)
    if old_last and old_last >= tag and not args.force:
        print(f"[OK] 本地数据({old_last})已覆盖 release {tag}，无需更新")
        return 0
    if args.force and old_last and old_last >= tag:
        print(f"[force] 本地已到 {old_last}，仍强制重下 release {tag}")

    with tempfile.TemporaryDirectory(dir=data_dir.parent) as tmp:
        work = Path(tmp)
        try:
            new_dir = download_and_extract(tag, work)
        except Exception as e:
            print(f"[FATAL] 下载/解压失败: {e}")
            return 1

        print("[2/3] 校验新数据")
        ok, problems = sanity_check(new_dir, data_dir, tag)
        for p in problems:
            print(f"  [QC] {p}")
        if not ok:
            print("[FATAL] 校验不通过，保留旧数据")
            return 1

        print("[3/3] 原子替换数据目录")
        atomic_swap(new_dir, data_dir)
        # 防止 ensure_qlib_data 因缺少 marker 用旧分卷覆盖刚下的 release
        marker = data_dir / ".abq_data_ready"
        marker.write_text(f"updated_by_update_daily tag={tag}\n")

    # 上传至 MinIO，供本地开发机自动同步
    try:
        from minio_sync import minio_settings, push_qlib, push_quant_data

        if minio_settings():
            push_qlib()
            push_quant_data()
    except Exception as exc:
        print(f"[minio] 上传失败（不影响本地更新）: {exc}")

    print(f"[OK] 数据已更新至 release {tag}（日历截止 {local_last_date(data_dir)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
