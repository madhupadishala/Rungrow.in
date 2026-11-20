import pytest
from fastapi import HTTPException
from app.main import enforce_product_scope
from app.registry import BY_ID

def test_product_growth_manager_requires_exact_product_scope():
    employee=BY_ID["pgm-photo-studio-growth-os"]
    with pytest.raises(HTTPException) as exc:
        enforce_product_scope(employee,None)
    assert exc.value.status_code==403
    with pytest.raises(HTTPException) as exc:
        enforce_product_scope(employee,"photo-studio-billing")
    assert exc.value.status_code==403

def test_product_growth_manager_accepts_its_assigned_product():
    enforce_product_scope(BY_ID["pgm-photo-studio-growth-os"],"photo-studio-growth-os")

def test_shared_employee_can_be_assigned_to_explicit_product():
    enforce_product_scope(BY_ID["market-intel"],"photo-studio-growth-os")
