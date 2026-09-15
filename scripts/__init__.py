"""Developer scripts: fixture recording and the pipeline snapshot generator.

A package so `tests/` imports `scripts.snapshot_pipeline` as a declared module rather
than relying on pytest inserting the rootdir into `sys.path`. Not shipped: the build
backend packages `src/digest` only.
"""
