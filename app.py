import base64
import json
import pathlib
import struct
import time
import traceback
import uuid

import aiofiles
import fastapi
import itsdangerous.timed
import pydantic
import pydantic_settings

import b64pickle

from typing import Annotated, Any, Literal, TypedDict, cast


###################################################################
### Stuff below is core inner workings.
###################################################################

ROOT_DIR = pathlib.Path(__file__).parent.resolve()


class Config(pydantic_settings.BaseSettings):
    class Main(pydantic.BaseModel):
        production: bool = False
        datadir: Annotated[pathlib.Path, pydantic.AfterValidator(lambda x: ROOT_DIR / x)] = pathlib.Path(
            ROOT_DIR, "data"
        )
        dbname: str = "analytics"
        secretkey: pydantic.SecretStr = pydantic.SecretStr("Incremental Game")
        expiry: int = 3600

    model_config = pydantic_settings.SettingsConfigDict(
        toml_file="config.toml",
        env_nested_delimiter="_",
        nested_model_default_partial_update=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[pydantic_settings.BaseSettings],
        init_settings: pydantic_settings.PydanticBaseSettingsSource,
        env_settings: pydantic_settings.PydanticBaseSettingsSource,
        dotenv_settings: pydantic_settings.PydanticBaseSettingsSource,
        file_secret_settings: pydantic_settings.PydanticBaseSettingsSource,
    ) -> tuple[pydantic_settings.PydanticBaseSettingsSource, ...]:
        return env_settings, dotenv_settings, pydantic_settings.TomlConfigSettingsSource(settings_cls)

    main: Main = pydantic.Field(default_factory=Main)


#####################################
### Pydantic models (and other) types
#####################################


class TokenData(pydantic.BaseModel):
    steam_id: int  # 64-bit integer
    uuid: uuid.UUID

    def encapsulate(self):
        return secret_serializer.dumps(self.model_dump(mode="json"))

    @classmethod
    def decapsulate(cls, data: str):
        return cls.model_validate(secret_serializer.loads(data, config.main.expiry))


class BaseResponse(pydantic.BaseModel):
    message: str


class AuthRequest(pydantic.BaseModel):
    steam_id: str
    # Note:
    random_value: Annotated[str, pydantic.Field(description="32-byte random data by client, base64-encoded")]
    """This is base64, but we cannot use pydantic.Base64Bytes due to FastAPI bug."""
    os: str
    os_version: str


class AuthResponse(BaseResponse):
    token: str
    expire: int


SendEventType = Literal[
    "start", "upgrade", "update", "end"  # Starting game  # Buying new upgrade  # Poll interval  # Ending game
]


class SendData(TypedDict):
    event: SendEventType
    playtime: int  # how much playtime has there been
    timestamp: int
    game_version: int
    scene: str
    save: dict[str, Any]


class SendDataWithID(SendData):
    player_id: str  # Deanonymized player ID derived from steam ID


####################################################################
### Functions for dependency injection and global state of variables
####################################################################

UUID_NAMESPACE = uuid.UUID(fields=(0x21401300, 0x2531, 0x0110, 0x42, 0x42, 0x494E4352474D), version=8)
COLLECTION_NAME = "analytics"


def uuid_from_steamid_and_value(steamid: int, random_value: bytes):
    inval = struct.pack("<Q", steamid) + random_value
    return uuid.uuid5(UUID_NAMESPACE, inval)


config = Config.model_validate({})
secret_serializer = itsdangerous.timed.TimedSerializer[str](
    config.main.secretkey.get_secret_value().encode("utf-8"), serializer=b64pickle
)

config.main.datadir.mkdir(parents=True, exist_ok=True)


async def get_token_data(encoded: Annotated[str, fastapi.Header(alias="X-Session-Token")]):
    try:
        return TokenData.decapsulate(encoded)
    except itsdangerous.BadSignature as e:
        raise fastapi.HTTPException(401, "Invalid token") from e


async def write_data(data: SendDataWithID):
    path = config.main.datadir / f"{time.time_ns()}_{data['player_id']}.json"
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        result = json.dumps(data, separators=(",", ":"))
        await f.write(result)


NeedTokenData = Annotated[TokenData, fastapi.Depends(get_token_data)]

##########################
### FastAPI app definition
##########################

_args = {}
if config.main.production:
    _args["openapi_url"] = None
    _args["docs_url"] = None
    _args["redoc_url"] = None
app = fastapi.FastAPI(title="Another Analytics", **_args)

#####################
### FastAPI endpoints
#####################


@app.post("/auth", status_code=201)
async def auth(request: AuthRequest, response: fastapi.Response) -> BaseResponse | AuthResponse:
    # TODO: Check if steam ID is valid and sane

    try:
        random_value = base64.b64decode(request.random_value)
    except ValueError:
        response.status_code = 400
        return BaseResponse(message="Invalid parameter")

    if not request.steam_id.isdigit() or len(request.steam_id) > 25 or len(random_value) != 32:
        response.status_code = 400
        return BaseResponse(message="Invalid parameter")

    steamid = int(request.steam_id)
    token = TokenData(steam_id=steamid, uuid=uuid_from_steamid_and_value(steamid, random_value))
    return AuthResponse(message="Ok", token=token.encapsulate(), expire=config.main.expiry)


@app.post("/send", status_code=200)
async def send(request: list[SendData], token: NeedTokenData) -> BaseResponse:
    for send_data in request:
        send_data_with_id = cast(SendDataWithID, send_data)
        send_data_with_id["player_id"] = str(token.uuid)

        try:
            await write_data(send_data_with_id)
        except Exception as e:
            traceback.print_exception(e)

    return BaseResponse(message="Ok")
