# Contributing to BnB AI Analyzer

Thank you for your interest in contributing to BnB AI Analyzer! This document provides guidelines and instructions for contributing to the project.

## Code Style

- Follow PEP 8 style guidelines for Python code
- Use meaningful variable and function names
- Add docstrings to all functions and classes
- Keep functions focused and single-purpose
- Use type hints where appropriate
- Include comments for complex logic

## Important Naming Convention

- Always use "abnb" instead of the original name throughout the codebase
- This applies to:
  - Variable names
  - Function names
  - Comments
  - Documentation
  - String literals

## Development Process

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Run tests if available
5. Update documentation as needed
6. Commit your changes (see commit message guidelines below)
7. Push to your fork
8. Open a Pull Request

## Commit Message Guidelines

Follow these conventions for commit messages:

```
type(scope): Brief description

Detailed description of changes if needed
```

Types:
- feat: New feature
- fix: Bug fix
- docs: Documentation changes
- style: Code style changes (formatting, missing semi-colons, etc)
- refactor: Code changes that neither fix bugs nor add features
- test: Adding or modifying tests
- chore: Changes to build process or auxiliary tools

## Pull Request Process

1. Update the README.md if your changes add new features or modify existing ones
2. Update documentation in the docs/ directory if needed
3. Ensure your code follows the project's code style
4. Make sure all tests pass
5. Update the CHANGELOG.md if applicable
6. Request review from maintainers

## File Organization

- Keep the project structure organized as outlined in README.md
- Place new search-related files in the `searches/` directory
- Store overlay files in the `overlays/` directory
- Add new configuration templates to the `templates/` directory
- Place documentation in the `docs/` directory

## Questions or Problems?

- Open an issue for bugs or feature requests
- Tag issues appropriately
- Provide as much relevant information as possible

## License

By contributing to BnB AI Analyzer, you agree that your contributions will be licensed under the same license as the project (see LICENSE file).
