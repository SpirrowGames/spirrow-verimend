"""config_keys: pydantic-settings fields and direct environment reads."""

from verimend.collector.extractors import config_keys

SETTINGS = '''
from typing import ClassVar

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAGICKIT_", extra="ignore")

    port: int = 8004
    lexora_url: str = Field(default="http://localhost:8001")
    token: str = Field(..., alias="GITHUB_TOKEN")
    names: list[str] = Field(default_factory=lambda: ["a"])
    required_key: str
    _private: int = 1
    VERSION: ClassVar[str] = "1"


class Extended(Settings):
    extra_flag: bool = False


class Legacy(BaseSettings):
    class Config:
        env_prefix = "old_"
        case_sensitive = True

    thing: str = "x"


class NotSettings:
    port: int = 1
'''

ENV = '''
import os
from os import environ, getenv

a = os.environ.get("A_URL", "http://x")
b = os.getenv("B_FLAG")
c = os.environ["C_SECRET"]
d = environ.get("D")
e = getenv("E", default="1")
f = os.environ.get(f"DYNAMIC_{a}")
os.environ["WRITTEN"] = "not a read"
'''


def _fields(tree):
    return {
        (f.content["class"], f.content["field"]): f.content
        for f in config_keys.extract(tree)
        if f.content["kind"] == "settings_field"
    }


def test_settings_fields_and_their_env_names(make_tree) -> None:
    fields = _fields(make_tree({"src/app/config.py": SETTINGS}))

    assert fields[("Settings", "port")]["env"] == "MAGICKIT_PORT"
    assert fields[("Settings", "port")]["default"] == "8004"
    assert fields[("Settings", "lexora_url")]["default"] == "'http://localhost:8001'"
    assert fields[("Settings", "token")]["env"] == "GITHUB_TOKEN"  # alias is not prefixed
    assert fields[("Settings", "token")]["default"] is None
    assert fields[("Settings", "names")]["default"] == "<factory: lambda: ['a']>"
    assert fields[("Settings", "required_key")]["default"] is None
    assert ("Settings", "_private") not in fields
    assert ("Settings", "VERSION") not in fields
    assert ("Settings", "model_config") not in fields


def test_prefix_is_inherited_and_v1_config_is_read(make_tree) -> None:
    fields = _fields(make_tree({"config.py": SETTINGS}))

    assert fields[("Extended", "extra_flag")]["env"] == "MAGICKIT_EXTRA_FLAG"
    assert fields[("Legacy", "thing")]["env"] == "old_thing"  # case_sensitive keeps case
    assert not any(cls == "NotSettings" for cls, _ in fields)


def test_settings_base_in_another_module(make_tree) -> None:
    tree = make_tree(
        {
            "base.py": 'from pydantic_settings import BaseSettings\n\nclass Base(BaseSettings):\n    model_config = {"env_prefix": "X_"}\n',
            "child.py": "from base import Base\n\nclass Child(Base):\n    port: int = 9000\n",
        }
    )
    envs = {c["field"]: c["env"] for c in (f.content for f in config_keys.extract(tree))}
    # a plain dict is as valid a model_config as SettingsConfigDict(...)
    assert envs == {"port": "X_PORT"}


def test_direct_environment_reads(make_tree) -> None:
    facts = [f for f in config_keys.extract(make_tree({"env.py": ENV})) if f.content["kind"] == "env_read"]
    reads = {f.content["env"]: f.content for f in facts}

    assert set(reads) == {"A_URL", "B_FLAG", "C_SECRET", "D", "E"}
    assert reads["A_URL"]["default"] == "'http://x'"
    assert reads["B_FLAG"]["default"] is None
    assert reads["C_SECRET"]["required"] is True
    assert reads["A_URL"]["required"] is False
    assert reads["E"]["default"] == "'1'"
    assert [f.line for f in facts] == sorted(f.line for f in facts)
