"""Application bootstrap helpers extracted from main.py.

These modules hold the non-routing concerns that used to live inline in the
FastAPI entrypoint: the KB-scraper reverse proxy, dev-server/admin-UI build
management, console logging setup, and the application lifespan handler.

Nothing here changes runtime behavior — it is a straight relocation so that
main.py can stay a thin assembler.
"""
