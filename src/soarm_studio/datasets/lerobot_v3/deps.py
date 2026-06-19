from __future__ import annotations


def require_pyarrow(*, purpose: str):
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError(f"pyarrow is required for {purpose}") from exc
    return pa, pq
