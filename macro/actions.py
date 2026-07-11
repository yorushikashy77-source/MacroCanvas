import copy

def iter_action_tree(actions):
    """Yield every action in stable pre-order, including sub-actions."""
    for action in actions or []:
        yield action
        yield from iter_action_tree(action.get("children", []))


def clone_action_tree(actions):
    """Deep-copy action data without copying Qt/runtime objects."""
    result = []
    for action in actions or []:
        copied = {
            key: copy.deepcopy(value)
            for key, value in dict(action).items()
            if key != "children"
        }
        copied["children"] = clone_action_tree(action.get("children", []))
        result.append(copied)
    return result
