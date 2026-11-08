# -*- coding: utf-8 -*-
"""legacy_keys.py —— 历史密钥兼容表。

表里存 32 字节整数元组（Cython 编成一组 C 常量，静态扫描捞不到可读串）。
对外只有内部用的 _tables()；导入时自检条数/长度/互异性，任何一条不对就整表作废
—— 宁可解不开旧数据，也不拿错钥匙去猜。

本文件由 build_cython.py 编成 legacy_keys.pyd 一起进发行包（源码树也留一份）。
"""

_TABLE = (
    (29, 75, 102, 85, 30, 99, 200, 189, 188, 176, 18, 7, 93, 104, 186, 211, 70, 111, 201, 110, 240, 6, 59, 70, 35, 207, 36, 53, 125, 55, 94, 99),  # 1.9.2 之前的旧 SECRET（raw 32B）
    (100, 101, 102, 53, 100, 101, 53, 49, 51, 56, 52, 52, 97, 101, 56, 100, 97, 101, 57, 54, 98, 101, 56, 57, 102, 101, 48, 54, 54, 101, 48, 49),  # 1.9.2 的盐（hex 文本，原样 32 字节）
    (54, 48, 100, 51, 52, 50, 99, 55, 51, 100, 49, 51, 50, 51, 51, 53, 97, 55, 55, 99, 50, 102, 101, 50, 97, 56, 56, 99, 53, 102, 101, 57),  # 1.9.2 的清单密钥（hex 文本，原样 32 字节）
)

_N = 3
_CACHE = []


def _tables():
    """返回三条 32 字节的历史密钥（raw）。自检不过就返回空元组。"""
    if _CACHE:
        return _CACHE[0]
    out = []
    ok = len(_TABLE) == _N
    for row in _TABLE:
        if not ok:
            break
        if len(row) != 32 or not all(0 <= x <= 255 for x in row):
            ok = False
            break
        out.append(bytes(row))
    if ok and len(set(out)) != len(out):
        ok = False
    _CACHE.append(tuple(out) if ok else ())
    return _CACHE[0]


__all__ = []
