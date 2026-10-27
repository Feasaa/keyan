"""业务逻辑层：可复用、可单测，不依赖 HTTP。

依赖方向单向：services 可依赖 schemas / core / prompts，不得 import app.api.*
"""
