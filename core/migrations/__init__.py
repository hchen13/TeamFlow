from importlib import import_module

MIGRATION_MODULES = (
    "001_initial",
    "002_workspace_workflow",
    "003_lark_app_name",
    "004_lark_app_name_synced_at",
    "005_lark_default_identity",
    "006_lark_identity_board_split",
    "007_workflow_descriptions",
    "008_general_workflow",
    "009_lark_app_avatar_url",
    "010_lark_user_identity",
    "011_lark_user_avatar_url",
)

MIGRATIONS = [import_module(f"{__name__}.{name}") for name in MIGRATION_MODULES]
