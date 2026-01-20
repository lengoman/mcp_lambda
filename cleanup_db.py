import boto3

def wipe_table():
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table('mcp-sessions')
    
    print("Scanning table...")
    response = table.scan()
    items = response.get('Items', [])
    
    print(f"Found {len(items)} items. Deleting...")
    
    with table.batch_writer() as batch:
        for item in items:
            batch.delete_item(
                Key={
                    'session_id': item['session_id'],
                    'timestamp': item['timestamp']
                }
            )
            
    print("Table wiped.")

if __name__ == "__main__":
    wipe_table()
