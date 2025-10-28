import base64
import pickle


def loads(data: str):
    rawdata = base64.urlsafe_b64decode(data)
    return pickle.loads(rawdata)


def dumps(obj):
    rawdata = pickle.dumps(obj)
    return str(base64.urlsafe_b64encode(rawdata), "utf-8")
