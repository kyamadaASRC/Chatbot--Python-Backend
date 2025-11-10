from typing import List, Dict


def plan_pipeline(draft: List[Dict[str, any]]):
    nodes = {s["cap"]: set(s.get("needs", [])) for s in draft}
    ordered = []
    ready = [k for k, v in nodes.items() if not v]
    while ready:
        n = ready.pop(0)
        ordered.append(next(s for s in draft if s["cap"] == n))
        for m in list(nodes.keys()):
            if n in nodes[m]:
                nodes[m].remove(n)
                if not nodes[m]:
                    ready.append(m)
        nodes.pop(n, None)
    if nodes:
        ordered += [s for s in draft if s["cap"] in nodes]
    return [ordered]