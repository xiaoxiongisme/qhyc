"""策略模块命名空间。

CL-20261117-06：此处**刻意保持无顶层 import**。
历史上这里曾 re-export fusion_signal 的 6 个符号，导致 `import app.strategies.<任意子模块>`
都会强制加载 1024 行的 fusion_signal（含 ORM 定义）。实测这 6 个 re-export 符号全仓零消费者，
而 V3.4 已于 CL-20261108-27 停用（保留代码与回切能力）。

需要这些符号的调用方请直接从定义模块 import：
    from app.strategies.fusion_signal import walk_fusion_states
    from app.strategies.registry import scan_candidates
"""
