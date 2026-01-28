
class HomeworksException(Exception):
    pass


class HomeworksConnectionFailed(HomeworksException):
    pass


class HomeworksConnectionLost(HomeworksException):
    pass


class HomeworksAuthenticationException(HomeworksException):
    pass


class HomeworksNoCredentialsProvided(HomeworksAuthenticationException):
    pass


class HomeworksInvalidCredentialsProvided(HomeworksAuthenticationException):
    pass