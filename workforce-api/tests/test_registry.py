from app.registry import EMPLOYEES,BY_ID,DEPARTMENTS
def test_department_and_role_registry():
    assert len(DEPARTMENTS)==11
    assert len(EMPLOYEES)>=40
    assert len(BY_ID)==len(EMPLOYEES)
def test_every_role_has_human_defined_contract():
    for e in EMPLOYEES:
        assert e.skills and e.permissions and e.mission and e.boundary


def test_digital_ceo_reports_to_founder_and_product_managers_are_mapped():
    ceo=BY_ID["digital-ceo"]
    assert ceo.manager=="Founder"
    assert "work.assign" in ceo.permissions
    product_managers=[e for e in EMPLOYEES if e.id.startswith("pgm-")]
    assert len(product_managers)==7
    assert len({e.product_id for e in product_managers})==7
    assert all(e.manager=="Digital CEO" and e.product_id for e in product_managers)
