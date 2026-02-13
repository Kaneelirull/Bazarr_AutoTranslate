#!/bin/bash

# Simple build and run script

echo "Building Bazarr AutoTranslate Docker image..."
docker-compose build

echo ""
echo "Build complete!"
echo ""
echo "To start the container:"
echo "  docker-compose up -d"
echo ""
echo "To view logs:"
echo "  docker-compose logs -f"
echo ""
echo "To stop:"
echo "  docker-compose down"
