from scout.names import compare_names, initials_compatible, last_name_key, normalize_name, split_name


def test_normalize_strips_accents_punctuation_and_suffixes():
    assert normalize_name("José  García-López, Jr.") == "jose garcia lopez"
    assert normalize_name("O'Reilly, Brendan PhD") == "oreilly brendan"


def test_split_name_handles_particles():
    assert split_name("ludwig van beethoven") == (["ludwig"], "van beethoven")
    assert last_name_key("ludwig van beethoven") == "beethoven"
    assert split_name("smith") == ([], "smith")


def test_initials_compatible():
    assert initials_compatible("j smith", "john smith")
    assert initials_compatible("j a smith", "john smith")
    assert not initials_compatible("jane smith", "john smith")
    assert not initials_compatible("john smith", "john smyth")
    assert initials_compatible("john smith", "john smith")


def test_compare_names_kinds():
    assert compare_names("jose garcia lopez", "jose garcia lopez")[0] == "exact"
    # spelling variants are deliberately NOT matched: they cannot be told apart from different names
    assert compare_names("jonathan smith", "johnathan smith")[0] == "none"
    assert compare_names("jon smith", "john smith")[0] == "none"
    assert compare_names("christopher l smith", "christopher lacy smith")[0] == "initials"
    assert compare_names("j smith", "john smith")[0] == "initials"
    assert compare_names("wei zhang", "john smith")[0] == "none"
    # one-letter differences in short given names are different people, not typos
    assert compare_names("dong zhang", "tong zhang")[0] == "none"
    assert compare_names("jifan zhang", "fan zhang")[0] == "none"
    assert compare_names("ting zhang", "tong zhang")[0] == "none"
    assert compare_names("jian li", "jin li")[0] == "none"
    assert compare_names("tingxu liu", "ting liu")[0] == "none"
    assert compare_names("yufeng wang", "yifeng wang")[0] == "none"
    # a fuzzy-looking string with a different last name must not match
    assert compare_names("john smith", "john smyth")[0] == "none"
