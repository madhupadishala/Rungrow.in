from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    role: str
    permissions: frozenset[str]
    actor_type: str = "human"
    # Populated only from verified, signed identity claims.
    product_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Decision:
    allowed: bool
    requires_approval: bool
    reason: str
    approval_id: str | None = None


SENSITIVE = {
    "message.send", "proposal.send", "pricing.change", "discount.apply",
    "contract.commit", "payment.initiate", "payment.refund", "data.export",
    "user.invite", "role.grant", "production.deploy", "campaign.activate",
    "customer.delete", "lead.bulk_contact", "legal.commit",
    "investor.commit", "investment.commit", "securities.commit", "data.sensitive_export", "access.control.change",
}


def evaluate(principal: Principal, permission: str, autonomy: str = "controlled") -> Decision:
    if not principal.user_id or not principal.tenant_id:
        return Decision(False, False, "Missing actor or tenant")
    if permission not in principal.permissions and "*" not in principal.permissions:
        return Decision(False, False, "Permission denied")
    if permission in SENSITIVE and (principal.actor_type == "agent" or autonomy == "controlled"):
        return Decision(False, True, "Human approval required", str(uuid4()))
    return Decision(True, False, "Allowed")


def enforce_tenant(principal: Principal, resource_tenant_id: str):
    if principal.tenant_id != resource_tenant_id:
        raise PermissionError("Cross-tenant access denied")
