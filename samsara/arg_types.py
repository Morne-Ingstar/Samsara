"""Static, effect-free argument validators owned by the declaring plugins.

Schema names are code declarations. Invocation args, config and model output
cannot install a validator; execution_policy decides which routes may use one.
"""

_ARG_TYPE_VALIDATORS = {}


def register_arg_type(name: str, validator) -> None:
    """Register a semantic validator without replacing primitive rules."""
    if not isinstance(name, str) or not name or name in {"int", "float", "bool", "str"}:
        raise ValueError("semantic type must have a non-primitive name")
    if not callable(validator):
        raise TypeError("semantic validator must be callable")
    previous = _ARG_TYPE_VALIDATORS.get(name)
    if previous is not None and previous is not validator and (
            previous.__module__, previous.__qualname__) != (
            validator.__module__, validator.__qualname__):
        raise ValueError(f"semantic type {name!r} is already registered")
    # Plugin reload recreates functions from the same static declaration.
    _ARG_TYPE_VALIDATORS[name] = validator


def type_ok(typ, value, *, app=None, allow_semantic=False) -> bool:
    if typ == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if typ == "bool":
        return isinstance(value, bool)
    if typ == "str":
        return isinstance(value, str)
    if not allow_semantic:
        return False      # model routes keep the original primitive-only rule
    validator = _ARG_TYPE_VALIDATORS.get(typ) if isinstance(typ, str) else None
    if validator is None:
        return False      # unregistered semantic names validate nothing
    try:
        return validator(value, app=app) is True
    except Exception:
        return False      # failed state read or broken validator: fail closed
