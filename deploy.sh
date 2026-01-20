#!/bin/bash
set -e

# Load environment variables
if [ -f .env ]; then
    set -a
    source .env
    set +a
else
    echo "Error: .env file not found"
    exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)


# 1. Create IAM Role if not exists
echo "Checking for IAM role..."
ROLE_ARN=$(aws iam get-role --role-name $ROLE_NAME --query 'Role.Arn' --output text 2>/dev/null || true)

if [ -z "$ROLE_ARN" ]; then
    echo "Creating IAM role..."
    TRUST_POLICY='{
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Principal": {
            "Service": "lambda.amazonaws.com"
          },
          "Action": "sts:AssumeRole"
        }
      ]
    }'
    ROLE_ARN=$(aws iam create-role --role-name $ROLE_NAME --assume-role-policy-document "$TRUST_POLICY" --query 'Role.Arn' --output text)
    echo "Attaching policies..."
    aws iam attach-role-policy --role-name $ROLE_NAME --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
    echo "Waiting for role propagation..."
    sleep 10
else
    echo "Role exists: $ROLE_ARN"
fi

# 2. Package
echo "Packaging..."
rm -rf package
mkdir package
# Note: Dependencies are provided via Lambda Layer (see deploy_layer.sh)


# Zip contents
# Dependencies are in the layer, so we just package the source files
echo "Zipping source files..."
# Ensure run.sh is executable
chmod +x run.sh

# Ensure run.sh has LF line endings for cross-platform support
echo "Preparing run.sh with LF line endings..."
python3 -c "
import os
if os.path.exists('run.sh'):
    with open('run.sh', 'rb') as f_in, open('run_clean.sh', 'wb') as f_out:
        content = f_in.read().replace(b'\r\n', b'\n')
        f_out.write(content)
"
chmod +x run_clean.sh

# Zip files - renaming run_clean.sh to run.sh inside the zip
# We swap the files temporarily
if [ -f "run_clean.sh" ]; then
    mv run.sh run.sh.bak
    mv run_clean.sh run.sh
    zip $ZIP_FILE server.py run.sh
    mv run.sh run_clean.sh
    mv run.sh.bak run.sh
    rm run_clean.sh
else
    zip $ZIP_FILE server.py run.sh
fi

# 3. Deployment
echo "Deploying to AWS Lambda..."

# Create DynamoDB Table if it doesn't exist
# Create DynamoDB Table if it doesn't exist

if ! aws dynamodb describe-table --table-name $TABLE_NAME --region $REGION >/dev/null 2>&1; then
    echo "Creating DynamoDB table $TABLE_NAME..."
    aws dynamodb create-table \
        --table-name $TABLE_NAME \
        --attribute-definitions AttributeName=session_id,AttributeType=S AttributeName=timestamp,AttributeType=N \
        --key-schema AttributeName=session_id,KeyType=HASH AttributeName=timestamp,KeyType=RANGE \
        --billing-mode PAY_PER_REQUEST \
        --region $REGION
    aws dynamodb wait table-exists --table-name $TABLE_NAME --region $REGION
fi

# Attach DynamoDB permissions to the role
echo "Attaching DynamoDB permissions..."
aws iam put-role-policy --role-name $ROLE_NAME --policy-name DynamoDBAccess --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "dynamodb:PutItem",
                "dynamodb:Query",
                "dynamodb:DeleteItem",
                "dynamodb:GetItem"
            ],
            "Resource": "arn:aws:dynamodb:'$REGION':'$ACCOUNT_ID':table/'$TABLE_NAME'"
        }
    ]
}'

ENV_VARS="Variables={AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap,RUST_LOG=info,PORT=8080,PYTHONUNBUFFERED=1,AWS_LWA_INVOKE_MODE=response_stream,TABLE_NAME=$TABLE_NAME}"

# Check if function exists
if aws lambda get-function --function-name $FUNCTION_NAME --region $REGION >/dev/null 2>&1; then
    echo "Function exists. Updating code..."
    aws lambda update-function-code \
        --function-name $FUNCTION_NAME \
        --zip-file fileb://$ZIP_FILE \
        --region $REGION >/dev/null
    
    echo "Waiting for code update..."
    aws lambda wait function-updated --function-name $FUNCTION_NAME --region $REGION

    echo "Updating configuration..."
    aws lambda update-function-configuration \
        --function-name $FUNCTION_NAME \
        --handler run.sh \
        --timeout 300 \
        --layers $LAYER_ARN \
        --environment "$ENV_VARS" \
        --region $REGION >/dev/null
else
    echo "Function does not exist. Creating..."
    aws lambda create-function \
        --function-name $FUNCTION_NAME \
        --runtime python3.11 \
        --role $ROLE_ARN \
        --handler run.sh \
        --zip-file fileb://$ZIP_FILE \
        --region $REGION \
        --timeout 300 \
        --layers $LAYER_ARN \
        --environment "$ENV_VARS" >/dev/null
fi

echo "Ensuring Function URL setup..."
if ! aws lambda get-function-url-config --function-name $FUNCTION_NAME --region $REGION >/dev/null 2>&1; then
    echo "Creating Function URL..."
    aws lambda create-function-url-config \
        --function-name $FUNCTION_NAME \
        --auth-type NONE \
        --invoke-mode RESPONSE_STREAM \
        --region $REGION
    
    aws lambda add-permission \
        --function-name $FUNCTION_NAME \
        --statement-id FunctionURLAllowPublicAccess \
        --action lambda:InvokeFunctionUrl \
        --principal "*" \
        --function-url-auth-type NONE \
        --region $REGION || true
else
    echo "Function URL already configured. Ensuring RESPONSE_STREAM mode..."
    aws lambda update-function-url-config \
        --function-name $FUNCTION_NAME \
        --invoke-mode RESPONSE_STREAM \
        --region $REGION >/dev/null
fi

FUNCTION_URL=$(aws lambda get-function-url-config --function-name $FUNCTION_NAME --region $REGION --query 'FunctionUrl' --output text)

# Update .env with LAMBDA_URL
if grep -q "^LAMBDA_URL=" .env; then
    # Update existing line
    sed "s|^LAMBDA_URL=.*|LAMBDA_URL=$FUNCTION_URL|" .env > .env.tmp && mv .env.tmp .env
else
    # Append to file
    echo "LAMBDA_URL=$FUNCTION_URL" >> .env
fi
echo "Updated .env with LAMBDA_URL=$FUNCTION_URL"

echo "Deployment complete! Function URL: $FUNCTION_URL"
echo "Note: The client may hang during handshake due to buffering. Ensure the client is using a streaming-capable HTTP client."
