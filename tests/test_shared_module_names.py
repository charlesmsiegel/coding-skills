"""The load_module fixture's eviction list must cover every shared basename.

`sys.modules` is keyed by bare module name, so two skills that both ship a
`rubric.py` share one cache slot. `conftest.load_module` evicts the names in
`SHARED_NAMES` around every import to keep them apart. A name missing from that
list does not fail loudly at the seam — it hands the second skill's tests the
first skill's module, and they fail with an AttributeError about a function
that exists perfectly well in the file the test names.

That failure only appears when both skills' tests run in the same session, so
`pytest tests/science_investigation` stayed green while `pytest -q` — CI — was
red. This test looks at the tree instead of at a run, so a newly shared
basename is reported the moment it is added.
"""


def test_every_shared_script_basename_is_evicted_between_skills(shared_names,
                                                                shared_basenames_on_disk):
    shared = shared_basenames_on_disk
    assert shared, "no shared basenames found at all — the scan is looking in the wrong place"
    missing = sorted(shared - set(shared_names))
    assert not missing, (
        f"{missing} are shipped by more than one skill's scripts/ directory but are absent "
        "from conftest.SHARED_NAMES, so tests for the second skill to import one will get "
        "the first skill's module out of sys.modules. Add them."
    )


def test_the_eviction_list_has_no_duplicate_entries(shared_names):
    assert len(shared_names) == len(set(shared_names))
