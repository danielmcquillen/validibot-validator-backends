"""Runtime metadata for the Portfolio Manager validator image."""

VALIDATOR_TYPE = "PORTFOLIO_MANAGER"
VALIDATOR_NAME = "Portfolio Manager Validator"
IMAGE_NAME = "validibot-validator-backend-portfolio-manager"
SUPPORTED_INPUT_TYPES = [
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/xml",
    "application/zip",
]
RESOURCE_REQUIREMENTS = {
    "memory_limit": "1Gi",
    "cpu_limit": "1.0",
    "timeout_seconds": 1500,
}
