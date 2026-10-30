FROM public.ecr.aws/lambda/python:3.12

# requirements.txt is exported from uv.lock with hashes, so the image installs the
# versions the tests ran against.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY common/ ${LAMBDA_TASK_ROOT}/common/
COPY market/ ${LAMBDA_TASK_ROOT}/market/
COPY strategy/ ${LAMBDA_TASK_ROOT}/strategy/
COPY scanner/ ${LAMBDA_TASK_ROOT}/scanner/
COPY executor/ ${LAMBDA_TASK_ROOT}/executor/
COPY monitor/ ${LAMBDA_TASK_ROOT}/monitor/
COPY closer/ ${LAMBDA_TASK_ROOT}/closer/
COPY reconciler/ ${LAMBDA_TASK_ROOT}/reconciler/

CMD ["scanner.handler.handler"]
