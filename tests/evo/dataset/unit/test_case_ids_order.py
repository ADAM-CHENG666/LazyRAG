from evo.operations.dataset.operations import _case_ids


def test_case_ids_orders_imported_then_generated_by_natural_case_id() -> None:
    case_ids = _case_ids({
        'stats': {'case_allocation': {'assignments': {
            'case_0010': {'mode': 'imported', 'source_row_number': 1},
            'case_0011': {'mode': 'generated'},
            'case_0001': {'mode': 'imported', 'source_row_number': 10},
            'case_0012': {'mode': 'generated'},
            'case_0002': {'mode': 'imported', 'source_row_number': 9},
        }}},
    })

    assert case_ids == ('case_0001', 'case_0002', 'case_0010', 'case_0011', 'case_0012')
