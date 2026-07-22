from importlib import import_module


MIGRATION_MODULES = ("001_initial",)
MIGRATIONS = [import_module(f"{__name__}.{name}") for name in MIGRATION_MODULES]
