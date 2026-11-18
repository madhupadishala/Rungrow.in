import pytest
from app.policy import Principal,evaluate,enforce_tenant
def test_read_permission_allowed():
    p=Principal("u1","t1","owner",frozenset({"reports.read"}))
    assert evaluate(p,"reports.read").allowed
def test_controlled_autonomy_requires_human_for_sensitive_action():
    p=Principal("u1","t1","owner",frozenset({"*"}))
    d=evaluate(p,"payment.initiate")
    assert d.requires_approval and not d.allowed and d.approval_id
def test_missing_permission_denied():
    p=Principal("u2","t1","viewer",frozenset())
    assert not evaluate(p,"reports.read").allowed
def test_cross_tenant_access_rejected():
    p=Principal("u1","t1","owner",frozenset({"*"}))
    with pytest.raises(PermissionError): enforce_tenant(p,"t2")
