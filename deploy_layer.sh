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

LAYER_NAME="${APP_NAME}-bundled-layer"
LAYER_ZIP="layer_bundled.zip"
ADAPTER_VERSION="v0.9.1"
ADAPTER_URL="https://github.com/awslabs/aws-lambda-web-adapter/releases/download/${ADAPTER_VERSION}/lambda-adapter-x86_64"

echo "Creating bundled layer: $LAYER_NAME"

# Clean up previous builds
rm -rf layer_content
rm -f $LAYER_ZIP

# Create directory structure
mkdir -p layer_content/python
mkdir -p layer_content/extensions

# 1. Install Python dependencies
echo "Installing dependencies (targeting Linux x86_64)..."
# Force installation of Linux-compatible binaries for AWS Lambda using pip
uv run python -m pip install -r requirements.txt --target layer_content/python --platform manylinux2014_x86_64 --only-binary=:all: --implementation cp --python-version 3.11 --upgrade

# 2. Get AWS Lambda Web Adapter (from Public Layer)
echo "Downloading AWS Lambda Web Adapter from public layer..."
# Public Layer ARN details
PUBLIC_LAYER_ARN="arn:aws:lambda:us-east-1:753240598075:layer:LambdaAdapterLayerX86"
PUBLIC_LAYER_VERSION="25"

# Get the download URL
LAYER_URL=$(aws lambda get-layer-version \
    --layer-name $PUBLIC_LAYER_ARN \
    --version-number $PUBLIC_LAYER_VERSION \
    --query 'Content.Location' \
    --output text)

# Download and unzip
curl -L -o layer_adapter.zip "$LAYER_URL"
unzip -o layer_adapter.zip -d layer_content
rm layer_adapter.zip

# 3. Ensure 'bootstrap' has LF line endings (Windows fix)
# Explicitly overwrite bootstrap to ensure it works cross-platform
echo "Enforcing LF line endings for bootstrap..."
uv run python -c "
with open('layer_content/bootstrap', 'wb') as f:
    f.write(b'#!/bin/sh\nexec \"\${LAMBDA_TASK_ROOT}/\${_HANDLER}\"\n')
"
chmod +x layer_content/bootstrap

# 4. Zip everything
echo "Zipping layer with explicit permissions..."
cd layer_content
uv run python -c "
import zipfile
import os
import stat

zip_filename = '../$LAYER_ZIP'
with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk('.'):
        # Add directories explicitly to ensure 755 permissions
        for d in dirs:
            dir_path = os.path.join(root, d)
            arcname = os.path.relpath(dir_path, '.')
            info = zipfile.ZipInfo(arcname + '/')  # trailing slash for directory
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            # Correct bitwise logic: (S_IFDIR | 0o755) << 16
            info.external_attr = (0o040000 | 0o755) << 16 
            zf.writestr(info, '')

        for file in files:
            file_path = os.path.join(root, file)
            arcname = os.path.relpath(file_path, '.')
            
            # Create ZipInfo instance
            info = zipfile.ZipInfo(arcname)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix
            
            # Set permissions
            # 0o755 (rwxr-xr-x) for executables, 0o644 (rw-r--r--) for others
            if arcname == 'bootstrap' or arcname.startswith('extensions/') or arcname.endswith('.sh') or arcname.endswith('.so'):
                perms = 0o755
            else:
                perms = 0o644
                
            # Correct bitwise logic: (S_IFREG | perms) << 16
            # 0o100000 is S_IFREG
            info.external_attr = (0o100000 | perms) << 16
            
            with open(file_path, 'rb') as f:
                zf.writestr(info, f.read())
"
cd ..

# 4. Publish Layer
echo "Publishing layer to AWS..."
LAYER_VERSION_ARN=$(aws lambda publish-layer-version \
    --layer-name $LAYER_NAME \
    --zip-file fileb://$LAYER_ZIP \
    --compatible-runtimes python3.11 \
    --region $REGION \
    --query 'LayerVersionArn' \
    --output text)

echo "Layer published: $LAYER_VERSION_ARN"

# 5. Update .env with new LAYER_ARN
if grep -q "^LAYER_ARN=" .env; then
    # Update existing line
    sed "s|^LAYER_ARN=.*|LAYER_ARN=$LAYER_VERSION_ARN|" .env > .env.tmp && mv .env.tmp .env
else
    # Append to file
    echo "LAYER_ARN=$LAYER_VERSION_ARN" >> .env
fi

echo "Updated .env with LAYER_ARN=$LAYER_VERSION_ARN"

# Cleanup
rm -rf layer_content
rm -f $LAYER_ZIP
