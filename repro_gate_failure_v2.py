
import re

def normalize(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\s*\(.*?\)$", "", s)
    s = s.split(" with ")[0].split(" from ")[0].split(" for ")[0].strip()
    if s.endswith("s") and len(s) > 4:
        s = s[:-1]
    return s

def check_interface_completeness_fuzzy(plan):
    contracts = plan.get("interface_contracts", [])
    all_provides = set()
    all_consumes = set()
    for contract in contracts:
        all_provides.update(contract.get("provides", []))
        all_consumes.update(contract.get("consumes", []))

    norm_provides = {normalize(p): p for p in all_provides}
    unresolved = []
    
    for u in all_consumes:
        nu = normalize(u)
        found = False
        if nu in norm_provides:
            found = True
        else:
            for np in norm_provides:
                if nu == np or (len(nu) > 3 and nu in np) or (len(np) > 3 and np in nu):
                    found = True
                    break
        if not found:
            unresolved.append(u)
            
    return len(unresolved) == 0, unresolved

plan_99 = {
    "interface_contracts": [
        {
            "provides": [
                "Plant dataclass with __post_init__ validation",
                "CareLevel enum (LOW, MEDIUM, HIGH)",
                "PlantValidationError exceptions with field-specific error messages"
            ],
            "consumes": []
        },
        {
            "provides": [],
            "consumes": [
                "Plant dataclass",
                "CareLevel enum",
                "PlantValidationError exception"
            ]
        }
    ]
}

passed, unresolved = check_interface_completeness_fuzzy(plan_99)
print(f"Fuzzy result for plan_99: {passed}, {unresolved}")
