
import sys
import os

# Mock the gate logic
def _is_primitive_or_stdlib(item: str):
    low = item.lower().strip()
    if low in ["int", "str", "float", "bool", "none"]:
        return True
    return False

def check_interface_completeness(plan):
    contracts = plan.get("interface_contracts", [])
    if not contracts:
        return True, "No contracts"

    all_provides = set()
    all_consumes = set()

    for contract in contracts:
        all_provides.update(contract.get("provides", []))
        all_consumes.update(contract.get("consumes", []))

    print(f"Provides: {all_provides}")
    print(f"Consumes: {all_consumes}")

    unresolved = all_consumes - all_provides
    if unresolved:
        filtered = {u for u in unresolved if _is_primitive_or_stdlib(u)}
        real_unresolved = unresolved - filtered
        if real_unresolved:
            return False, f"Unresolved: {real_unresolved}"
    
    return True, "Passed"

# Case from task-1776662046.990939
plan_99 = {
    "interface_contracts": [
        {
            "component": "plant_data",
            "provides": [
                "Plant dataclass with __post_init__ validation",
                "CareLevel enum (LOW, MEDIUM, HIGH)",
                "PlantValidationError exceptions with field-specific error messages"
            ],
            "consumes": []
        },
        {
            "component": "test_plant_error_cases",
            "provides": [],
            "consumes": [
                "Plant dataclass",
                "CareLevel enum",
                "PlantValidationError exception"
            ]
        }
    ]
}

passed, msg = check_interface_completeness(plan_99)
print(f"Result for plan_99: {passed}, {msg}")
