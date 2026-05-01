from browser.lender_scripts.base import BaseLenderScript, ApplyResult
from browser.lender_scripts.uline import UlineScript
from browser.lender_scripts.brex import BrexScript
from browser.lender_scripts.fundbox import FundboxScript
from browser.lender_scripts.nav import NavScript
from browser.lender_scripts.generic import GenericScript

SCRIPT_REGISTRY = {
    "uline": UlineScript,
    "brex": BrexScript,
    "fundbox": FundboxScript,
    "nav": NavScript,
    "generic": GenericScript,
}


def get_script(script_name: str) -> type:
    return SCRIPT_REGISTRY.get(script_name, GenericScript)
