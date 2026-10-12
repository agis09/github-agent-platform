"""In-sandbox workers run by the Kubernetes Jobs (deploy/jobs/*.yaml).

Each worker reads its inputs from the environment, runs the configured
agent runtime once, and prints its result between log markers (a completed
pod rejects exec/cp, so the pod log is the durable collection path):

- plan     PLAN_RESULT_BEGIN / PLAN_RESULT_END
- execute  EXECUTION_RESULT_BEGIN / _END and PATCH_BEGIN / PATCH_END
- review   REVIEW_RESULT_BEGIN / REVIEW_RESULT_END
"""
