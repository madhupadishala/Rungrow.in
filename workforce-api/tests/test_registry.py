from app.registry import EMPLOYEES,BY_ID,DEPARTMENTS
def test_department_and_role_registry():
    assert len(DEPARTMENTS)==11
    assert len(EMPLOYEES)>=40
    assert len(BY_ID)==len(EMPLOYEES)
def test_every_role_has_human_defined_contract():
    for e in EMPLOYEES:
        assert e.skills and e.permissions and e.mission and e.boundary
