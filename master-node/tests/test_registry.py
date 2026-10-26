import pytest

from master_node.config import load_settings
from master_node.registry import RegistryError, load_registry

LAB = """
[[instrument]]
id = "thor"
kind = "transport"
driver = "manual"

[[instrument]]
id = "flex"
kind = "liquid_handler"
driver = "opentrons_flex"
url = "http://172.25.68.5:31950"
plate_slots = ["C2", "D2"]
default_slot = "D2"

[[instrument]]
id = "tecan"
kind = "plate_reader"
driver = "tecan_infinite"
url = "http://192.168.0.116:8000"
[instrument.limits]
modes = ["absorbance"]
"""


def write(tmp_path, text: str):
    path = tmp_path / "instruments.toml"
    path.write_text(text)
    return path


def test_loads_the_lab_registry(tmp_path):
    registry = load_registry(write(tmp_path, LAB))
    assert [i.id for i in registry.instruments] == ["thor", "flex", "tecan"]
    assert registry.transport.driver == "manual"
    assert registry.get("flex").default_slot == "D2"
    assert registry.get("tecan").default_slot == "carrier"
    assert registry.get("tecan").limits == {"modes": ["absorbance"]}
    assert registry.get("tecan").name == "Tecan"


def test_missing_file_means_the_default_simulated_lab(tmp_path):
    registry = load_registry(tmp_path / "none.toml")
    assert [(i.id, i.driver) for i in registry.instruments] == [("thor", "sim"), ("flex", "sim"), ("tecan", "sim")]


@pytest.mark.parametrize("change, message", [
    (('driver = "tecan_infinite"', 'driver = "opentrons_flex"'), "is for a liquid_handler, not a plate_reader"),
    (('url = "http://192.168.0.116:8000"', ''), "needs a url"),
    (('kind = "plate_reader"', 'kind = "spectrometer"'), "kind must be one of"),
    (('id = "tecan"', 'id = "flex"'), "listed twice"),
    (('default_slot = "D2"', 'default_slot = "A1"'), "default_slot 'A1' is not in plate_slots"),
    (('driver = "manual"', 'driver = "manual"\ncolour = "red"'), "unknown keys ['colour']"),
])
def test_rejects_mistakes(tmp_path, change, message):
    with pytest.raises(RegistryError, match=message.replace("[", r"\[").replace("]", r"\]")):
        load_registry(write(tmp_path, LAB.replace(*change)))


def test_old_device_sections_in_config_point_to_the_registry(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[flex]\nmode = "http"\n')
    with pytest.raises(ValueError, match=r"\[flex\] now belong in the instrument registry"):
        load_settings(config)
