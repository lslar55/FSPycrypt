class FSDecryptError(Exception):
    pass


class InvalidContainerError(FSDecryptError):
    pass


class MissingKeyError(FSDecryptError):
    pass


class InvalidKeyFileError(FSDecryptError):
    pass


class InvalidExfatError(FSDecryptError):
    pass


class InvalidNtfsError(FSDecryptError):
    pass


class InvalidVhdError(FSDecryptError):
    pass
