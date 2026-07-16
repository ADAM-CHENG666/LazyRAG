from .errors import DefinitionError


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be str')
    return value


def _text(value: object, name: str) -> None:
    value = _string(value, name)
    if not value.strip():
        raise DefinitionError(f'{name} must be non-empty')


def _positive_int(value: object, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f'{name} must be int')
    if value < 1:
        raise DefinitionError(f'{name} must be >= 1')


def _positive_number(value: float, name: str) -> None:
    if value <= 0:
        raise DefinitionError(f'{name} must be positive')


__all__ = ['_positive_int', '_positive_number', '_string', '_text']
