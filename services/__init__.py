"""JobOS package bootstrap: load local configuration before submodules run."""
from services.common.config import load_repo_env

load_repo_env()
