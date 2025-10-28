import contextlib
import struct
import uuid

import fastapi
import itsdangerous.timed
import pydantic
import pydantic_settings
import pymongo
import pymongo.asynchronous.collection

import b64pickle

from typing import Annotated, Literal, TypedDict, cast


def _empty_list_is_empty_dict(value):
    if isinstance(value, list) and len(value) == 0:
        return {}
    return value


EmptyListIsDict = pydantic.BeforeValidator(_empty_list_is_empty_dict)


#################################
### Begin of configurable things.
#################################


# If the save schema changes, modify this.
class SaveData(TypedDict):
    resources: Annotated[dict[str, float], EmptyListIsDict]
    prestige: int
    playtime: float
    idletime: float
    prestigeLevels: list[int]
    stats: Annotated[dict[str, int | float], EmptyListIsDict]
    metrics: Annotated[dict[str, int | float], EmptyListIsDict]
    upgradeLevels: Annotated[dict[str, int], EmptyListIsDict]


###################################################################
### End of configurable things. Stuff below is core inner workings.
###################################################################


class Config(pydantic_settings.BaseSettings):
    class Main(pydantic.BaseModel):
        dburl: pydantic.SecretStr = pydantic.SecretStr("mongodb://localhost:27017/")
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
    random_value: pydantic.Base64Bytes
    os: str
    os_version: str


class AuthResponse(BaseResponse):
    token: str


SendEventType = Literal[
    "start", "upgrade", "update", "end"  # Starting game  # Buying new upgrade  # Poll interval  # Ending game
]


class SendData(TypedDict):
    event: SendEventType
    playtime: int  # how much playtime has there been
    timestamp: int  # time it was collected
    game_version: int
    scene: str
    save: SaveData


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
mongo_client: pymongo.AsyncMongoClient[SendDataWithID] | None = None
print(config)


async def mongodb_dependency():
    if mongo_client is None:
        raise RuntimeError("mongodb is None")

    await mongo_client.aconnect()
    db = mongo_client[config.main.dbname]

    if COLLECTION_NAME not in await db.list_collection_names():
        col = await db.create_collection(COLLECTION_NAME, timeseries={"timeField": "timestamp"})
    else:
        col = db.get_collection(COLLECTION_NAME)

    yield col


async def get_token_data(encoded: Annotated[str, fastapi.Header(alias="X-Session-Token")]):
    return TokenData.decapsulate(encoded)


@contextlib.asynccontextmanager
async def begin_transaction():
    if mongo_client is None:
        raise RuntimeError("mongodb is None")

    async with mongo_client.start_session() as session, await session.start_transaction():
        yield session


NeedMongoDB = Annotated[
    pymongo.asynchronous.collection.AsyncCollection[SendDataWithID], fastapi.Depends(mongodb_dependency)
]
NeedTokenData = Annotated[TokenData, fastapi.Depends(get_token_data)]

##########################
### FastAPI app definition
##########################


@contextlib.asynccontextmanager
async def lifetime(app: fastapi.FastAPI):
    global mongo_client
    async with pymongo.AsyncMongoClient(config.main.dburl.get_secret_value()) as client:
        mongo_client = client
        yield
    mongo_client = None


app = fastapi.FastAPI(title="Another Analytics", lifespan=lifetime)

#####################
### FastAPI endpoints
#####################


@app.post("/auth", status_code=201)
async def auth(request: AuthRequest, response: fastapi.Response) -> BaseResponse | AuthResponse:
    # TODO: Check if steam ID is valid and sane

    if not request.steam_id.isdigit() or len(request.steam_id) > 25 or len(request.random_value) != 32:
        response.status_code = 400
        return BaseResponse(message="Invalid parameter")

    steamid = int(request.steam_id)
    token = TokenData(steam_id=steamid, uuid=uuid_from_steamid_and_value(steamid, request.random_value))
    return AuthResponse(message="Ok", token=token.encapsulate())


@app.post("/send", status_code=200)
async def send(request: list[SendData], token: NeedTokenData, db: NeedMongoDB) -> BaseResponse:
    async with begin_transaction() as sess:
        for send_data in request:
            send_data_with_id = cast(SendDataWithID, send_data)
            send_data_with_id["player_id"] = str(token.uuid)
            await db.insert_one(send_data_with_id, session=sess)

    return BaseResponse(message="Ok")
