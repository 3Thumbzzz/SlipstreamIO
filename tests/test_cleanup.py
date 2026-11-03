r"""Clean up: what the scan finds, what it refuses, and what applying moves.

The scan exists because a filename can carry text that was meant to be a
character -- ``Justice - Safe &amp; Sound.mp3`` is six characters of entity text
where an ampersand was intended, and 440 files in the reported library are like
that. The app reads them correctly as they stand (``test_entity_paths.py``);
this is the offer to rename them to what they meant.

Three properties matter more than the detection itself:

* **A blocked finding is reported, not hidden.** "6 I cannot fix, and why" is
  the useful answer; dropping them silently is not.
* **A percent-escape that decodes to a control character is not an escape.**
  ``BIXIGA 70 7pol 100%13`` is "100 percent 13" and must not be touched.
* **A track is relinked only once its file has really moved**, so a failed
  rename leaves that track untouched everywhere rather than pointing at a name
  that does not exist.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.model import cleanup                        # noqa: E402
from trackcleaner.model.library import Track, TRAKTOR         # noqa: E402
from trackcleaner.model import matching                       # noqa: E402

DIR = r"D:\Music\Beatjunkies"


def _t(name: str) -> Track:
    path = os.path.join(DIR, name)
    return Track(key=matching.path_key(path), path=path, source=TRAKTOR)


def _scan(names, present=None, **kw):
    """Scan ``names``, treating ``present`` (default: all of them) as on disk."""
    tracks = [_t(n) for n in names]
    on_disk = {os.path.join(DIR, n).lower()
               for n in (names if present is None else present)}
    return cleanup.scan(tracks, exists=lambda p: p.lower() in on_disk, **kw)


# ============================== detection =================================
@pytest.mark.parametrize("entity,char", [
    ("&amp;", "&"), ("&apos;", "'"), ("&#39;", "'"),
])
def test_entity_text_in_a_filename_is_found_and_decoded(entity, char):
    found = _scan([f"Safe {entity} Sound.mp3"])

    assert [f.kind for f in found] == ["xml_entity_filename"]
    assert found[0].proposed_name == f"Safe {char} Sound.mp3"
    assert found[0].applicable


def test_an_ordinary_name_is_not_a_finding():
    assert _scan(["Bacao Rhythm & Steel Band.mp3", "plain.mp3"]) == []


def test_entity_text_in_the_directory_is_left_alone():
    """Renaming a folder moves every track inside it -- a different operation
    with different risks, not one to fold in silently."""
    path = os.path.join(r"D:\Music\AC&amp;DC", "plain.mp3")
    track = Track(key=matching.path_key(path), path=path, source=TRAKTOR)

    assert cleanup.scan([track], exists=lambda p: True) == []


def test_percent_escapes_are_their_own_kind():
    found = _scan(["Rock%20Steady.mp3"])

    assert [f.kind for f in found] == ["percent_escape_filename"]
    assert found[0].proposed_name == "Rock Steady.mp3"


def test_a_percent_that_is_not_an_escape_is_not_touched():
    """The real counterexample: ``BIXIGA 70 7pol 100%13`` is "100 percent 13".
    ``%13`` decodes to a control character, so it was never an escape."""
    assert _scan(["BIXIGA 70 7pol 100%13.wav"]) == []


def test_scanning_can_be_narrowed_to_one_kind():
    names = ["Safe &amp; Sound.mp3", "Rock%20Steady.mp3"]

    found = _scan(names, kinds=["xml_entity_filename"])

    assert [f.kind for f in found] == ["xml_entity_filename"]


# =============================== blocking =================================
def test_a_name_windows_cannot_hold_is_reported_but_not_offered():
    """``&quot;`` really does decode to ``"``, which no Windows filename may
    contain. Six tracks in the reported library are in that state."""
    found = _scan(['She Said &quot;No&quot;.mp3'])

    assert len(found) == 1, "still reported"
    assert not found[0].applicable
    assert '"' in found[0].blocked


def test_a_missing_file_is_reported_but_not_offered():
    found = _scan(["Safe &amp; Sound.mp3"], present=[])

    assert not found[0].applicable
    assert found[0].blocked == "the file is not on disk"


def test_a_name_already_taken_is_reported_but_not_offered():
    found = _scan(["Safe &amp; Sound.mp3", "Safe & Sound.mp3"])
    entity = next(f for f in found if "&amp;" in f.current_name)

    assert not entity.applicable
    assert entity.blocked == "a different file is already called that"


def test_two_tracks_renaming_onto_one_file_are_both_blocked():
    """Only visible across the whole scan, never per track."""
    found = _scan(["Safe &amp; Sound.mp3", "Safe &#38; Sound.mp3"])

    assert len(found) == 2
    assert all(not f.applicable for f in found)
    assert all("same file" in f.blocked for f in found)


# ================================ applying =================================
def test_applying_returns_the_relink_map_keyed_by_track():
    found = _scan(["Safe &amp; Sound.mp3"])
    calls = []

    moved, failures = cleanup.rename_files(
        found, rename=lambda a, b: calls.append((a, b)))

    assert not failures
    assert calls == [(found[0].current, found[0].proposed)]
    assert moved == {found[0].track_key: found[0].proposed}


def test_a_rename_that_fails_relinks_nothing_for_that_track():
    """The half-applied state this guards against: the database pointing at a
    name that is not on disk."""
    found = _scan(["Safe &amp; Sound.mp3", "Toss It Up &amp; Away.mp3"])

    def rename(a, b):
        if "Toss" in a:
            raise OSError(13, "Permission denied")

    moved, failures = cleanup.rename_files(found, rename=rename)

    assert list(moved) == [found[0].track_key], "only the one that moved"
    assert [f.current_name for f, _ in failures] == ["Toss It Up &amp; Away.mp3"]
    assert "Permission denied" in failures[0][1]


def test_a_blocked_finding_is_never_attempted():
    found = _scan(['She Said &quot;No&quot;.mp3'])
    calls = []

    moved, failures = cleanup.rename_files(found, rename=lambda a, b: calls.append(a))

    assert calls == [] and moved == {} and failures == []


# ============================== presentation ===============================
def test_findings_group_by_kind_in_detector_order():
    found = _scan(["Rock%20Steady.mp3", "Safe &amp; Sound.mp3"])

    assert list(cleanup.group(found)) == ["xml_entity_filename",
                                          "percent_escape_filename"]


def test_every_kind_has_a_heading_and_an_explanation():
    """The dialog renders these, so a kind with no words is a blank section."""
    for detector in cleanup.DETECTORS:
        assert detector.label and detector.explain
        assert cleanup.DETECTORS_BY_KIND[detector.kind] is detector


# ===================== an accent recorded the other way =====================
# The reported library: Traktor recorded ``Déja Vu`` with a decomposed accent
# (e + U+0301) while the file on disk is named with the composed one (U+00E9).
# They look identical, NTFS treats them as two names, and the track read as
# missing although it was right there.
NFD_NAME = "Dogg Master - Déja Vu.mp3"
NFC_NAME = "Dogg Master - Déja Vu.mp3"
RELINK = "unicode_form_path"


def test_a_name_recorded_in_the_other_spelling_is_relinked_to_the_disk():
    found = _scan([NFD_NAME], present=[NFC_NAME])

    assert [f.kind for f in found] == [RELINK]
    assert found[0].proposed == os.path.join(DIR, NFC_NAME)
    assert found[0].applicable


def test_it_works_in_the_other_direction_too():
    """The disk decides, not a normal form: a composed record whose file is
    named decomposed is relinked to the decomposed spelling."""
    found = _scan([NFC_NAME], present=[NFD_NAME])

    assert found[0].proposed == os.path.join(DIR, NFD_NAME)


def test_a_recorded_spelling_that_exists_is_the_file():
    """``Tony! Toni! Toné!`` is on disk twice, one file per spelling. Whichever
    one a library records is a real file and must be left alone."""
    assert _scan([NFD_NAME], present=[NFD_NAME, NFC_NAME]) == []


def test_a_file_missing_in_both_spellings_is_not_a_finding():
    """Nothing to relink to -- that track is genuinely gone, and the integrity
    scan already says so."""
    assert _scan([NFD_NAME], present=[]) == []


def test_an_ascii_name_is_never_stat_ed():
    """Most of a library is ASCII and has only one spelling; asking the disk
    about each of 80,000 tracks would be the whole cost of the scan."""
    asked = []
    track = _t("plain.mp3")

    cleanup.scan([track], kinds=[RELINK], exists=lambda p: asked.append(p))

    assert asked == []


def test_both_other_spellings_on_disk_is_ambiguous():
    """A path mixing both forms has two other spellings. If both are files,
    nothing says which one the library meant."""
    mixed = "Rémi - Caé.mp3"              # one decomposed, one composed
    import unicodedata
    present = [unicodedata.normalize("NFC", mixed), unicodedata.normalize("NFD", mixed)]

    found = _scan([mixed], present=present)

    assert len(found) == 1 and not found[0].applicable
    assert "both spellings" in found[0].blocked


def test_a_relink_that_would_duplicate_an_entry_is_blocked():
    """The reported Traktor collection holds one track twice: a missing
    decomposed entry beside a present composed one. Relinking the first would
    leave two entries for one file -- and with no other program to relink,
    there is nothing left to apply."""
    found = _scan([NFD_NAME], present=[NFC_NAME])
    nfd_key = found[0].track_key
    nfc_key = matching.path_key(os.path.join(DIR, NFC_NAME))

    blocked = cleanup.block_existing_entries(
        found, {"Traktor": {nfd_key, nfc_key}, "VirtualDJ": set()})

    assert not blocked[0].applicable
    assert blocked[0].blocked.startswith("Traktor already has an entry")


def test_a_program_holding_both_spellings_sits_the_relink_out():
    """The reported Señorita: VirtualDJ held the composed entry (with its cues)
    *and* a bare decomposed stub its playlists named, while Traktor held only
    the decomposed one and showed the track missing. Traktor is relinked;
    VirtualDJ is left as it is rather than given a duplicate."""
    found = _scan([NFD_NAME], present=[NFC_NAME])
    nfd_key = found[0].track_key
    nfc_key = matching.path_key(os.path.join(DIR, NFC_NAME))

    kept = cleanup.block_existing_entries(
        found, {"Traktor": {nfd_key}, "VirtualDJ": {nfd_key, nfc_key}})

    assert kept[0].applicable
    assert kept[0].skipped == ("VirtualDJ",)
    assert cleanup.relink_map(kept) == {nfd_key: kept[0].proposed}
    assert cleanup.skipped_keys(kept, "VirtualDJ") == {nfd_key}
    assert cleanup.skipped_keys(kept, "Traktor") == set()
    assert cleanup.skipped_note(kept[0]).startswith("VirtualDJ left as is")


def test_a_playlist_reference_beside_the_entry_is_not_a_duplicate():
    """VirtualDJ's own shape: ``database.xml`` holds the composed entry while
    a ``.vdjfolder`` still names the decomposed path. Only collection entries
    are passed in, so relinking the playlist reference is allowed -- it is
    exactly what reunites the two."""
    found = _scan([NFD_NAME], present=[NFC_NAME])
    nfc_key = matching.path_key(os.path.join(DIR, NFC_NAME))

    kept = cleanup.block_existing_entries(found, {"VirtualDJ": {nfc_key}})

    assert kept[0].applicable


def test_a_relink_never_renames_a_file():
    """The file is already right; renaming it would be the bug."""
    found = _scan([NFD_NAME], present=[NFC_NAME])
    calls = []

    moved, failures = cleanup.rename_files(
        found, rename=lambda a, b: calls.append((a, b)))

    assert calls == [] and moved == {} and failures == []
    assert cleanup.relink_map(found) == {found[0].track_key: found[0].proposed}


def test_a_blocked_relink_is_not_in_the_map():
    found = cleanup.block_existing_entries(
        _scan([NFD_NAME], present=[NFC_NAME]),
        {"Traktor": {matching.path_key(os.path.join(DIR, NFD_NAME)),
                     matching.path_key(os.path.join(DIR, NFC_NAME))}})

    assert cleanup.relink_map(found) == {}


def test_the_difference_is_spelled_out_in_code_points():
    """Both dialog columns show the same text, so this is the only visible
    evidence of what will change."""
    found = _scan([NFD_NAME], present=[NFC_NAME])

    assert cleanup.spelling_difference(found[0]) == "e + U+0301 → U+00E9"


def test_rename_kinds_ignore_the_duplicate_check():
    found = _scan(["Safe &amp; Sound.mp3"])

    assert cleanup.block_existing_entries(found, {"Traktor": set()}) == found


# ================================ the dialog ===============================
@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_dialog_offers_only_applicable_findings_of_the_ticked_kinds(app):
    """A blocked finding is never returned however its section is ticked -- the
    dialog reports it so the count adds up, not so it gets attempted."""
    from trackcleaner.ui.cleanup_dialog import CleanupDialog
    found = _scan(["Safe &amp; Sound.mp3", 'She Said &quot;No&quot;.mp3',
                   "Rock%20Steady.mp3"])
    dialog = CleanupDialog(found, "“Temp” (3 tracks)")

    dialog._boxes["xml_entity_filename"].setChecked(True)
    dialog._boxes["percent_escape_filename"].setChecked(True)

    assert [f.current_name for f in dialog.chosen_findings()] == [
        "Safe &amp; Sound.mp3", "Rock%20Steady.mp3"]


def test_a_kind_can_be_left_alone(app):
    """The reason each kind gets its own checkbox: "fix the ampersands, leave
    the percent signs" is a real answer."""
    from trackcleaner.ui.cleanup_dialog import CleanupDialog
    found = _scan(["Safe &amp; Sound.mp3", "Rock%20Steady.mp3"])
    dialog = CleanupDialog(found, "“Temp”")

    dialog._boxes["percent_escape_filename"].setChecked(False)

    assert [f.kind for f in dialog.chosen_findings()] == ["xml_entity_filename"]


def test_a_section_with_nothing_fixable_cannot_be_ticked(app):
    from trackcleaner.ui.cleanup_dialog import CleanupDialog
    dialog = CleanupDialog(_scan(['She Said &quot;No&quot;.mp3']), "“Temp”")

    box = dialog._boxes["xml_entity_filename"]

    assert not box.isEnabled() and not box.isChecked()
    assert dialog.chosen_findings() == []


def test_a_relink_section_names_what_differs(app):
    from trackcleaner.ui.cleanup_dialog import CleanupDialog
    dialog = CleanupDialog(_scan([NFD_NAME], present=[NFC_NAME]), "“Set”")

    assert [f.kind for f in dialog.chosen_findings()] == [RELINK]
    from PySide6.QtWidgets import QTreeWidget
    tree = dialog.findChild(QTreeWidget)
    assert tree.headerItem().text(1) == "On disk"
    assert tree.topLevelItem(0).text(2) == "e + U+0301 → U+00E9"


def test_a_clean_library_says_so(app):
    from trackcleaner.ui.cleanup_dialog import CleanupDialog
    dialog = CleanupDialog([], "“Temp” (2 tracks)")

    assert dialog.chosen_findings() == []
    assert not dialog._ok.isEnabled(), "nothing to apply"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
