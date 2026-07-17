"""确保训练用 Qlib cn_data 就绪：从 datasets/ 分卷包解压。

GitHub 单文件 ≤100MB，故把 ~839MB 的 cn_data 打成 gzip 分卷：
  datasets/qlib_cn_data.tar.gz.part_aa ...
克隆后首次训练/推理会自动拼接校验并解压到 datasets/qlib_data/cn_data。

用法：
    python quant/ops/ensure_qlib_data.py
    python quant/ops/ensure_qlib_data.py --force
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DATASETS = REPO / "datasets"
PART_GLOB = "qlib_cn_data.tar.gz.part_*"
SHA_FILE = DATASETS / "qlib_cn_data.tar.gz.sha256"
EXTRACT_ROOT = DATASETS / "qlib_data"          # 解压后：datasets/qlib_data/cn_data
MARKER = EXTRACT_ROOT / "cn_data" / ".abq_data_ready"


def part_files() -> list[Path]:
    parts = sorted(DATASETS.glob(PART_GLOB))
    if not parts:
        raise FileNotFoundError(
            f"未找到分卷数据 {DATASETS}/{PART_GLOB}；请 git pull 完整仓库或手动放入分卷。"
        )
    return parts


def expected_sha() -> str | None:
    if not SHA_FILE.exists():
        return None
    return SHA_FILE.read_text().strip().split()[0]


def concat_sha(parts: list[Path]) -> str:
    h = hashlib.sha256()
    for p in parts:
        with p.open("rb") as f:
            while True:
                chunk = f.read(8 * 1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
    return h.hexdigest()


def already_ready() -> bool:
    cal = EXTRACT_ROOT / "cn_data" / "calendars" / "day.txt"
    return MARKER.exists() and cal.exists()


def extract(force: bool = False) -> Path:
    out = EXTRACT_ROOT / "cn_data"
    if already_ready() and not force:
        print(f"[OK] Qlib 数据已就绪: {out}")
        return out

    parts = part_files()
    print(f"[1/3] 找到 {len(parts)} 个分卷，合计 "
          f"{sum(p.stat().st_size for p in parts) / 1e6:.0f} MB")

    exp = expected_sha()
    if exp:
        print("[2/3] 校验 sha256 …")
        got = concat_sha(parts)
        if got != exp:
            raise RuntimeError(
                f"分卷校验失败：期望 {exp}，实际 {got}。请重新 git pull / 下载分卷。"
            )
        print(f"  sha256 OK ({got[:12]}…)")
    else:
        print("[2/3] 无 sha256 文件，跳过校验")

    if EXTRACT_ROOT.exists() and force:
        shutil.rmtree(EXTRACT_ROOT)
    EXTRACT_ROOT.mkdir(parents=True, exist_ok=True)

    print(f"[3/3] 解压到 {EXTRACT_ROOT} …（约 1–3 分钟）")
    # 流式拼接分卷 → tar.gz → 解压
    cat = subprocess.Popen(
        ["cat", *[str(p) for p in parts]],
        stdout=subprocess.PIPE,
    )
    assert cat.stdout is not None
    with tarfile.open(fileobj=cat.stdout, mode="r|gz") as tf:
        tf.extractall(path=EXTRACT_ROOT)
    rc = cat.wait()
    if rc != 0:
        raise RuntimeError(f"cat 分卷失败 exit={rc}")

    if not (EXTRACT_ROOT / "cn_data" / "calendars" / "day.txt").exists():
        raise RuntimeError("解压后缺少 calendars/day.txt，数据包可能损坏")

    MARKER.write_text("ready\n")
    print(f"[OK] 解压完成: {out}")
    return out


def resolve_provider_uri(configured: str | None = None, *, ensure: bool = True) -> str:
    """返回可用的绝对 provider_uri。

    优先仓库内解压目录 datasets/qlib_data/cn_data；
    否则解析配置路径（相对路径相对仓库根，~ 会 expanduser）。
    """
    if ensure:
        extract(force=False)
    bundled = EXTRACT_ROOT / "cn_data"
    if (bundled / "calendars" / "day.txt").exists():
        return str(bundled.resolve())
    if configured:
        p = Path(configured).expanduser()
        if not p.is_absolute():
            p = (REPO / p).resolve()
        else:
            p = p.resolve()
        return str(p)
    return str(bundled.resolve())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="强制重新解压")
    args = ap.parse_args()
    try:
        extract(force=args.force)
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 1
    print(f"provider_uri → {resolve_provider_uri()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
