# Lambda Web Adapter bootstrap shim.
#
# LWA's /opt/bootstrap (set via AWS_LAMBDA_EXEC_WRAPPER) intercepts the
# invocation before Lambda ever calls this function, so it never runs.
# CloudFormation's EarlyValidation requires a valid module.function handler
# for python3.12 runtimes — this satisfies that check.


def handler(event, context):  # pragma: no cover
    raise RuntimeError("LWA bootstrap should have intercepted this invocation")
