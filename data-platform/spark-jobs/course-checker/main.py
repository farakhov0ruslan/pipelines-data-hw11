"""
Spark-джоб проверки качества данных курсовых работ.
Читает датасет из MinIO (S3), прогоняет набор проверок, сохраняет результаты.
"""
import os
import sys
from pyspark.sql import SparkSession
from pyspark.sql import functions as F


def build_spark_session() -> SparkSession:
    return (
        SparkSession.builder
        .appName("course-checker")
        .config("spark.hadoop.fs.s3a.endpoint", os.environ["S3_ENDPOINT"])
        .config("spark.hadoop.fs.s3a.access.key", os.environ["S3_ACCESS_KEY"])
        .config("spark.hadoop.fs.s3a.secret.key", os.environ["S3_SECRET_KEY"])
        .config("spark.hadoop.fs.s3a.path.style.access", "true")
        .getOrCreate()
    )


def run_checks(spark: SparkSession, input_path: str) -> dict:
    df = spark.read.parquet(input_path)

    total = df.count()
    nulls = {c: df.filter(F.col(c).isNull()).count() for c in df.columns}
    duplicates = total - df.dropDuplicates().count()

    return {
        "total_rows": total,
        "null_counts": nulls,
        "duplicate_rows": duplicates,
        "status": "OK" if duplicates == 0 and all(v == 0 for v in nulls.values()) else "FAILED",
    }


def main():
    input_path = os.environ.get("INPUT_PATH", "s3a://data-bucket/input/dataset.parquet")
    output_path = os.environ.get("OUTPUT_PATH", "s3a://data-bucket/output/check-result.json")

    spark = build_spark_session()
    try:
        result = run_checks(spark, input_path)
        print(f"[course-checker] status={result['status']} total={result['total_rows']}")

        spark.createDataFrame([result]).write.mode("overwrite").json(output_path)
        sys.exit(0 if result["status"] == "OK" else 1)
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
